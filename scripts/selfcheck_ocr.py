#!/usr/bin/env python3
"""OCR 降级发送适配器（方案 D）自检。可在 Windows 与其它平台运行。

默认 dry-run：只用 Fake 驱动走一遍定位 / 暂停 / 幂等，不会截取真实屏幕，
也不会点击或向微信粘贴。即使加 --live，也只检查 Windows 依赖能否导入，
仍不会对真实窗口执行点击（本骨架尚未验证）。

用法（在仓库根目录）：
    python scripts/selfcheck_ocr.py
    python scripts/selfcheck_ocr.py --config config/example.toml
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from random import Random


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OCR 降级发送自检（默认 dry-run）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument(
        "--live",
        action="store_true",
        help="尝试导入 Windows 真实驱动/引擎（仍不会点击；非 Windows 或缺依赖会说明）",
    )
    args = parser.parse_args(argv)
    root = _root()
    os.chdir(root)
    sys.path.insert(0, str(root))

    print("======== OCR 适配器自检（dry-run，不会真实点击） ========")
    ok = True

    try:
        from src.adapters.ocr import (
            CaptchaOrDialogError,
            FakeOcrEngine,
            FakeScreenDriver,
            OcrOutboundAdapter,
            WindowsScreenDriver,
        )
        from src.adapters.pause import PauseGate, SendPausedError
        from src.clock import FakeClock
        from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter

        print("OK   模块导入成功（未在顶层加载 pyautogui / OCR 引擎）")
    except Exception as exc:  # noqa: BLE001
        print("FAIL 模块导入失败:", exc)
        return 1

    if args.config:
        try:
            from src.config_util import load_config

            load_config(Path(args.config), project_root=root)
            print(f"OK   配置: 已加载 {args.config}")
        except Exception as exc:  # noqa: BLE001
            print("FAIL 配置:", exc)
            ok = False
    else:
        print("OK   配置: 使用内置默认")

    data_dir = root / "data" / "selfcheck"
    data_dir.mkdir(parents=True, exist_ok=True)
    pause = PauseGate(data_dir=data_dir)
    # 自检不留下暂停文件。
    if pause.is_paused():
        pause.resume()

    clock = FakeClock(datetime(2026, 11, 11, 12, 0, 0))
    spec = BucketSpec(capacity=8, refill_per_second=1.0)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=spec,
            per_group=spec,
            announcement=spec,
            auto_reply=spec,
        ),
        clock=clock,
    )
    driver = FakeScreenDriver(
        elements=[
            {"text": "交易会员群演示", "x": 80, "y": 20, "width": 100, "height": 20, "confidence": 0.99},
            {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 20, "confidence": 0.99},
            {"text": "发送", "x": 360, "y": 400, "width": 40, "height": 20, "confidence": 0.99},
        ]
    )
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,  # 自检直接走 Fake，不装配 HumanizedSender
        driver=driver,
        engine=FakeOcrEngine(scripted=[{"text": "交易会员群演示 输入 发送", "confidence": 0.99}]),
        limiter=limiter,
        pause=pause,
        data_dir=data_dir,
        clock=clock,
        rng=Random(1),
        dry_run=True,
        group_names={"demo-group": "交易会员群演示"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    try:
        adapter.send_text("demo-group", "自检消息（不会真实发送）")
    except Exception as exc:  # noqa: BLE001
        print("FAIL dry-run 发送失败:", exc)
        ok = False
    else:
        if driver.clicks or driver.pastes:
            print("FAIL dry-run 不应产生点击或粘贴:", driver.clicks, driver.pastes)
            ok = False
        elif adapter.history and adapter.history[-1].status == "dry_run":
            print("OK   Fake dry-run 走通：已定位且未点击")
        else:
            print("FAIL dry-run 状态异常:", adapter.history)
            ok = False

    # 暂停开关
    pause.pause("selfcheck")
    blocked = False
    try:
        adapter.send_text("demo-group", "暂停后不应发送")
    except SendPausedError:
        blocked = True
    except Exception as exc:  # noqa: BLE001
        print("FAIL 暂停检查异常:", exc)
        ok = False
    if blocked:
        print(f"OK   紧急暂停生效（{pause.path}）")
    else:
        print("FAIL 存在 PAUSE 文件时仍尝试发送")
        ok = False
    pause.resume()

    # 验证码检测（另建适配器，避免幂等窗口干扰）
    captcha_driver = FakeScreenDriver(
        elements=[{"text": "请完成验证", "x": 10, "y": 10, "confidence": 0.99}],
        dialogs=["验证码"],
    )
    captcha_adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,  # 自检直接走 Fake，不装配 HumanizedSender
        driver=captcha_driver,
        engine=FakeOcrEngine(scripted=[{"text": "请完成验证", "confidence": 0.99}]),
        limiter=limiter,
        data_dir=data_dir,
        clock=clock,
        dry_run=True,
        confirm_group_name=False,
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    try:
        captcha_adapter.send_text("demo-group", "遇到验证码")
        print("FAIL 发现验证码时未暂停")
        ok = False
    except CaptchaOrDialogError:
        if pause.is_paused():
            print("OK   验证码检测会写入 PAUSE 并停止")
        else:
            print("FAIL 验证码异常未创建暂停文件")
            ok = False
    pause.resume()

    if args.live:
        try:
            WindowsScreenDriver()
            print("WARN 已实例化 WindowsScreenDriver，但本骨架尚未在真实微信上验证，不会执行点击")
        except Exception as exc:  # noqa: BLE001
            print("OK   Windows 真实驱动按预期给出说明:", exc)
    else:
        print("OK   跳过真实 Windows 驱动（默认 dry-run；需要时加 --live，仍不会点击）")

    print("说明：OCR 降级发送延迟可能达十余秒，只应作为 UIA 失败后的备选。")
    print("======== 自检", "通过" if ok else "未通过", "========")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

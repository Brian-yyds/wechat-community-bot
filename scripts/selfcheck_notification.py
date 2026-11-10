#!/usr/bin/env python3
"""通知预览适配器（方案 C）自检。可在 Windows 与其它平台运行。

默认 dry-run：只检查导入、配置、假通知源与唤醒钩子，不连接系统通知中心，
也不读取真实微信会话列表。

用法（在仓库根目录）：
    python scripts/selfcheck_notification.py
    python scripts/selfcheck_notification.py --config config/example.toml
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通知预览适配器自检（默认 dry-run）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument(
        "--live",
        action="store_true",
        help="尝试实例化 Windows 真实源（仍不会把结果当作已验证；非 Windows 会失败并说明）",
    )
    args = parser.parse_args(argv)
    root = _root()
    os.chdir(root)
    sys.path.insert(0, str(root))

    print("======== 通知适配器自检（dry-run） ========")
    ok = True

    try:
        from src.adapters.notification import (
            FakeNotificationSource,
            NotificationAdapter,
            WindowsNotificationSource,
        )
        from src.clock import FakeClock
        from datetime import datetime

        print("OK   模块导入成功（未在顶层加载 Windows 依赖）")
    except Exception as exc:  # noqa: BLE001
        print("FAIL 模块导入失败:", exc)
        return 1

    # 配置（可选）
    cfg_msg = "未指定，使用内置默认"
    if args.config:
        try:
            from src.config_util import load_config

            load_config(Path(args.config), project_root=root)
            cfg_msg = f"已加载 {args.config}"
        except Exception as exc:  # noqa: BLE001
            print("FAIL 配置:", exc)
            ok = False
        else:
            print("OK   配置:", cfg_msg)
    else:
        print("OK   配置:", cfg_msg)

    clock = FakeClock(datetime(2026, 11, 11, 12, 0, 0))
    source = FakeNotificationSource(clock=clock)
    woken: list[str] = []
    adapter = NotificationAdapter(
        source=source,
        clock=clock,
        wake_callback=lambda hint: woken.append(hint.group),
    )
    if adapter.is_complete_source() or adapter.completeness_guaranteed():
        print("FAIL 完整性声明错误：通知适配器不得声称自己是全量源")
        ok = False
    else:
        print("OK   完整性声明：is_complete_source=False")

    source.push("交易会员群演示", "自检预览（虚构）", sender="演示用户")
    events = []
    adapter.subscribe(events.append)
    hints = adapter.poll_once()
    partial = all(
        e.message and e.message.extra.get("partial") is True
        for e in events
        if e.message
    )
    if hints and woken == ["交易会员群演示"] and partial:
        print(f"OK   Fake 源产出 {len(hints)} 条 partial 预览，并触发唤醒回调")
    else:
        print("FAIL Fake 源或唤醒回调异常:", hints, woken, partial)
        ok = False

    # Windows 骨架：默认只说明，不假装验证。
    if args.live:
        try:
            WindowsNotificationSource()
            print("WARN 已实例化 WindowsNotificationSource，但本骨架尚未在真实微信上验证")
        except Exception as exc:  # noqa: BLE001
            print("OK   Windows 真实源按预期给出说明:", exc)
    else:
        print("OK   跳过真实 Windows 源（默认 dry-run；需要时加 --live）")

    print("======== 自检", "通过" if ok else "未通过", "========")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

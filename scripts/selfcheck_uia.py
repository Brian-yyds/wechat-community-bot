#!/usr/bin/env python3
"""适配器 B（UIA 发送）自检。

可在 Windows 与 Linux box 上运行。默认 dry-run：只探测环境，不向任何会话发送。
真实发送仅在用户明确传入 --send-test 且确认语正确时，发到「文件传输助手」。

用法（仓库根目录）：
    python scripts/selfcheck_uia.py
    python scripts/selfcheck_uia.py --send-test --confirm yes-send-to-file-helper

本脚本不会绕过限速层去群发，也不会自动重登或处理验证码。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

MIN_PYTHON = (3, 11)
CONFIRM_PHRASE = "yes-send-to-file-helper"
DEFAULT_TARGET = "文件传输助手"


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _print(ok: bool | None, title: str, msg: str) -> None:
    if ok is True:
        tag = "OK  "
    elif ok is False:
        tag = "FAIL"
    else:
        tag = "INFO"
    print(tag, title + ":", msg)


def check_python() -> tuple[bool, str]:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return ok, f"Python {v.major}.{v.minor}.{v.micro}（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）"


def check_platform() -> tuple[bool, str]:
    if sys.platform == "win32":
        return True, "Windows，可以进行真实 UIA 探测（仍默认 dry-run）"
    return True, f"当前平台 {sys.platform}，只能跑 FakeUiDriver 模拟，不能驱动官方 PC 微信"


def check_deps() -> tuple[bool, str, dict]:
    sys.path.insert(0, str(_root()))
    from src.adapters.pyweixin_driver import describe_backend

    info = describe_backend()
    if sys.platform != "win32":
        return True, "非 Windows，跳过真实依赖安装检查（模拟路径可用）", info
    if info.get("pyweixin") or info.get("pywinauto"):
        backend = info.get("backend")
        ver = info.get("pyweixin_version") or info.get("pywinauto_version") or ""
        return True, f"检测到可选依赖 {backend} {ver}".strip(), info
    return False, info.get("hint") or "未安装 pyweixin / pywinauto", info


def check_pause(root: Path, config: dict) -> tuple[bool, str]:
    from src.adapters.uia import UiaConfig

    cfg = UiaConfig.from_mapping(config, project_root=root)
    flag = cfg.resolve_pause_flag(root)
    if flag.exists():
        # dry-run 只提示，不把自检整单打失败；真实发送路径会拒绝。
        return True, f"注意：紧急暂停标记存在: {flag}（发送将被拒绝，确认安全后删除再恢复）"
    return True, f"未发现暂停标记（{flag}）"


def check_windows_runtime() -> tuple[bool, str, dict]:
    """在 Windows 上检查微信进程 / 窗口 / UIA 可达；其他平台返回说明。"""
    sys.path.insert(0, str(_root()))
    result: dict = {"process": None, "window": None, "uia": None}
    if sys.platform != "win32":
        return True, "非 Windows，跳过进程与窗口检查", result
    from src.adapters.pyweixin_driver import PyWeixinDriver

    # 进程名探测（不依赖 pywinauto 也能做）
    names = {"weixin.exe", "wechat.exe", "wechatapp.exe"}
    found = []
    try:
        import subprocess

        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True,
            errors="ignore",
            timeout=8,
        )
        for line in out.splitlines():
            low = line.lower()
            for n in names:
                if n in low and n not in found:
                    found.append(n)
    except Exception as exc:  # noqa: BLE001
        result["process_error"] = str(exc)
    result["process"] = found
    driver = PyWeixinDriver()
    try:
        probe = driver.probe()
    except Exception as exc:  # noqa: BLE001
        probe = {"error": str(exc)}
    result["probe"] = probe
    result["window"] = probe.get("window_visible")
    result["uia"] = bool(probe.get("chat_window_found"))
    parts = [
        "微信进程: " + (", ".join(found) if found else "未发现"),
        f"窗口可见: {probe.get('window_visible')}",
        f"已登录: {probe.get('logged_in')}",
        f"锁屏: {probe.get('locked')}",
        f"UIA 可达: {probe.get('chat_window_found')}",
        f"阻断弹窗: {probe.get('blocking_dialog')}",
    ]
    if probe.get("error"):
        parts.append(f"探测错误: {probe.get('error')}")
    parts.append("注意：以上探测不能代替真实验收，本驱动尚未在真实微信上验证。")
    ok = bool(found) and bool(probe.get("window_visible")) and bool(probe.get("chat_window_found"))
    return ok, "；".join(parts), result


def load_config(root: Path, config_path: Path | None) -> dict:
    sys.path.insert(0, str(root))
    from src.config_util import load_config

    try:
        return load_config(config_path, project_root=root) or {}
    except Exception:  # noqa: BLE001
        return {}


def run_fake_timings(root: Path, config: dict) -> tuple[bool, str, list[dict]]:
    """用 FakeUiDriver 走一遍已打开 / 未打开会话，输出分段耗时并对照目标。"""
    sys.path.insert(0, str(root))
    from src.adapters.ui_driver import FakeUiDriver
    from src.adapters.uia import UiaConfig, UiaOutboundAdapter

    cfg = UiaConfig.from_mapping(config, project_root=root)
    # 自检打点用极小输入停顿，避免脚本本身睡很久；真实拟人延迟仍由限速层负责。
    # 模拟发送的状态文件必须落在临时目录，不得写入仓库 data/PAUSE。
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="uia-selfcheck-"))
    cfg.data_dir = str(scratch)
    cfg.pause_flag = str(scratch / "PAUSE")
    cfg.input_pause_min = 0.0
    cfg.input_pause_max = 0.0
    cfg.group_switch_interval_min = 0.0
    cfg.group_switch_interval_max = 0.0
    cfg.min_send_interval_seconds = 0.0
    cfg.preheat_on_start = False
    cfg.keep_chat_open = False
    cfg.return_to_primary_after_send = False
    rows: list[dict] = []

    def _run(title: str, already_open: bool) -> dict:
        driver = FakeUiDriver(known_chats={"交易会员群-主群", "文件传输助手"})
        adapter = UiaOutboundAdapter(driver=driver, config=cfg, project_root=scratch)
        if already_open:
            driver.open_chat("交易会员群-主群")
        t0 = time.perf_counter()
        result = adapter.send("交易会员群-主群", "自检打点（虚构，请忽略）", send_id=f"selfcheck-{title}")
        wall = time.perf_counter() - t0
        target = cfg.target_open_chat_seconds if already_open else cfg.target_unopened_chat_seconds
        row = {
            "case": title,
            "ok": result.ok,
            "status": result.status,
            "chat_was_open": result.timings.chat_was_open,
            "adapter_total": round(result.timings.adapter_total, 4),
            "wall": round(wall, 4),
            "open_chat": round(result.timings.open_chat, 4),
            "paste": round(result.timings.paste, 4),
            "send": round(result.timings.send, 4),
            "verify": round(result.timings.verify, 4),
            "input_pause": round(result.timings.input_pause, 4),
            "group_switch_wait": round(result.timings.group_switch_wait, 4),
            "target_seconds": target,
            "meets_target": result.timings.adapter_total <= target,
        }
        rows.append(row)
        return row

    opened = _run("already_open", True)
    unopened = _run("unopened", False)
    ok = opened["ok"] and unopened["ok"] and opened["meets_target"] and unopened["meets_target"]
    lines = []
    for row in rows:
        flag = "达标" if row["meets_target"] else "未达标"
        lines.append(
            f"{row['case']}: adapter_total={row['adapter_total']}s "
            f"(目标 ≤{row['target_seconds']}s, {flag})；"
            f"open_chat={row['open_chat']} paste={row['paste']} "
            f"send={row['send']} verify={row['verify']} "
            f"chat_was_open={row['chat_was_open']}"
        )
    lines.append("以上为 FakeUiDriver 模拟耗时，不含限速层 2–6 秒拟人延迟；真实 Windows 耗时请用 dry-run 观察窗口操作后再决定是否 --send-test。")
    return ok, " | ".join(lines), rows


def maybe_real_send(args, root: Path, config: dict) -> tuple[bool, str]:
    if not args.send_test:
        return True, "未指定 --send-test，保持 dry-run，没有真实发送"
    if sys.platform != "win32":
        return False, "非 Windows 拒绝真实发送。请在用户电脑上运行本脚本。"
    if args.confirm != CONFIRM_PHRASE:
        return (
            False,
            f"--send-test 需要同时传入 --confirm {CONFIRM_PHRASE}，"
            "并且只允许发到「文件传输助手」，以防止误发到会员群。",
        )
    target = args.target or DEFAULT_TARGET
    if target != DEFAULT_TARGET:
        return False, f"真实发送目标必须是「{DEFAULT_TARGET}」，当前为 {target}，已拒绝。"
    from src.adapters.uia import UiaConfig, UiaOutboundAdapter
    from src.adapters.pyweixin_driver import PyWeixinDriver

    cfg = UiaConfig.from_mapping(config, project_root=root)
    flag = cfg.resolve_pause_flag(root)
    if flag.exists():
        return False, f"存在紧急暂停标记 {flag}，拒绝发送"
    adapter = UiaOutboundAdapter(driver=PyWeixinDriver(), config=cfg, project_root=root)
    result = adapter.send(
        target,
        "【自检】wechat-community-bot UIA 发送测试，请忽略。",
        timeout=cfg.send_timeout_seconds,
        send_id=None,
    )
    t = result.timings.as_dict()
    msg = (
        f"status={result.status} ok={result.ok} error={result.error or '无'}；"
        f"分段耗时 {t}"
    )
    return result.ok, msg


def UiaPause(root, config):  # 占位避免上面误用，不会被调用
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="适配器 B（UIA 发送）自检")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--send-test", action="store_true", help="向文件传输助手发送一条测试（需确认）")
    parser.add_argument(
        "--confirm",
        default="",
        help=f"与 --send-test 同时使用，必须恰好为 {CONFIRM_PHRASE}",
    )
    parser.add_argument("--target", default=DEFAULT_TARGET, help="仅允许「文件传输助手」")
    args = parser.parse_args(argv)
    root = _root()
    os.chdir(root)
    sys.path.insert(0, str(root))

    print("======== 适配器 B 自检开始 ========")
    py_ok, py_msg = check_python()
    _print(py_ok, "Python", py_msg)
    plat_ok, plat_msg = check_platform()
    _print(plat_ok, "平台", plat_msg)
    dep_ok, dep_msg, dep_info = check_deps()
    _print(dep_ok, "依赖", dep_msg)
    if dep_info.get("hint"):
        _print(None, "依赖说明", dep_info["hint"])

    config = load_config(root, Path(args.config) if args.config else None)
    pause_ok, pause_msg = check_pause(root, config)
    _print(pause_ok, "暂停标记", pause_msg)

    win_ok, win_msg, _win = check_windows_runtime()
    _print(win_ok if sys.platform == "win32" else None, "微信/UIA", win_msg)

    time_ok, time_msg, _rows = run_fake_timings(root, config)
    _print(time_ok, "模拟分段耗时", time_msg)

    send_ok, send_msg = maybe_real_send(args, root, config)
    _print(send_ok, "发送", send_msg)

    print("延迟目标：已打开会话适配器自身耗时 ≤ 3s；未打开会话 ≤ 8s（不含限速层拟人延迟）。")
    print("部署注意：请用专用小号、家庭网络常驻桌面会话，窗口可见、不锁屏、不远程断连。详见 docs/ADAPTER_B.md。")

    # 非 Windows 上「依赖 / 微信进程」检查以说明为主，不作为失败。
    required = [py_ok, plat_ok, pause_ok, time_ok, send_ok]
    if sys.platform == "win32":
        required.extend([dep_ok, win_ok])
    ok = all(required)
    print("======== 适配器 B 自检", "通过" if ok else "未通过", "========")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

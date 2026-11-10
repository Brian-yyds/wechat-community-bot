#!/usr/bin/env python3
"""一键自检：Python 版本、依赖、配置、目录权限、限速参数、模拟端到端。

可在 Windows 与其他平台运行。不访问真实微信，也不打印密钥。

用法（在仓库根目录）：
    python scripts/selfcheck.py
    python scripts/selfcheck.py --config config/example.toml
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def check_python() -> tuple[bool, str]:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return ok, f"Python {v.major}.{v.minor}.{v.micro}（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）"


def check_deps() -> tuple[bool, str]:
    missing = []
    # tomllib / json 为标准库；pyyaml、pytest 为可选
    try:
        import tomllib  # noqa: F401
    except ImportError:
        missing.append("tomllib")
    extras = []
    try:
        import yaml  # noqa: F401
        extras.append("pyyaml")
    except ImportError:
        extras.append("pyyaml(未安装，YAML 配置不可用)")
    try:
        import pytest  # noqa: F401
        extras.append("pytest")
    except ImportError:
        extras.append("pytest(未安装，不影响运行演示)")
    if missing:
        return False, "缺少：" + ", ".join(missing)
    return True, "标准库可用；" + "、".join(extras)


def check_config(root: Path, config_path: Path | None) -> tuple[bool, str, dict]:
    sys.path.insert(0, str(root))
    from src.config_util import ConfigError, find_config_path, load_config

    try:
        path = find_config_path(config_path, project_root=root)
        data = load_config(config_path, project_root=root)
    except ConfigError as exc:
        return False, str(exc), {}
    except Exception as exc:  # noqa: BLE001
        return False, f"配置加载失败: {exc}", {}
    where = str(path) if path else "（空配置）"
    return True, f"已加载 {where}", data


def check_dirs(root: Path, config: dict) -> tuple[bool, str]:
    bot = config.get("bot") or {}
    data_dir = Path(bot.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".selfcheck_write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"数据目录不可写: {data_dir} ({exc})"
    return True, f"数据目录可写: {data_dir}"


def check_rate(config: dict) -> tuple[bool, str]:
    section = config.get("rate_limit") or {}
    problems = []
    hour = int(section.get("global_replies_per_hour", 40))
    if hour <= 0 or hour > 500:
        problems.append(f"global_replies_per_hour={hour} 不合理（建议 1～500，默认 40）")
    lo = float(section.get("send_delay_min", 2.0))
    hi = float(section.get("send_delay_max", 6.0))
    if lo < 1.5:
        problems.append(f"send_delay_min={lo} 低于 1.5 秒")
    if not (lo < hi <= 8.0):
        problems.append(f"拟人延迟区间 ({lo}, {hi}) 须满足下限 < 上限 ≤ 8")
    night = section.get("night_silent") or {}
    policy = str(night.get("auto_reply_policy", "drop"))
    if night.get("enabled", True) and policy != "drop":
        problems.append(f"夜间静默开启时建议 auto_reply_policy=drop，当前为 {policy}")
    anns = config.get("announcements") or []
    for item in anns:
        jitter = float(item.get("jitter_seconds", 3))
        if jitter > 5:
            problems.append(f"公告 {item.get('id')} jitter_seconds={jitter} 超过准点 ±5 秒窗口")
    if problems:
        return False, "；".join(problems)
    return True, f"限速参数合理（每小时回复上限 {hour}，延迟 {lo}–{hi}s）"


def check_demo(root: Path, data_dir: Path) -> tuple[bool, str]:
    sys.path.insert(0, str(root))
    from datetime import datetime
    from random import Random

    from src.clock import FakeClock
    from src.main import run_demo

    try:
        summary = run_demo(
            project_root=root,
            data_dir=data_dir,
            clock=FakeClock(datetime(2026, 11, 11, 9, 0, 5)),
            rng=Random(1),
            demo_fast=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"模拟端到端失败: {exc}"
    sent = len(summary.get("sent") or [])
    results = len(summary.get("results") or [])
    return True, f"模拟端到端通过：处理 {results} 条，发出 {sent} 条"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交易会员群机器人自检")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args(argv)
    root = _root()
    os.chdir(root)
    checks: list[tuple[str, tuple]] = []

    print("======== 自检开始 ========")
    py_ok, py_msg = check_python()
    print(("OK  " if py_ok else "FAIL"), "Python:", py_msg)
    dep_ok, dep_msg = check_deps()
    print(("OK  " if dep_ok else "FAIL"), "依赖:", dep_msg)
    cfg_ok, cfg_msg, config = check_config(root, Path(args.config) if args.config else None)
    print(("OK  " if cfg_ok else "FAIL"), "配置:", cfg_msg)
    dir_ok, dir_msg = check_dirs(root, config) if cfg_ok else (False, "跳过（配置未加载）")
    print(("OK  " if dir_ok else "FAIL"), "目录:", dir_msg)
    rate_ok, rate_msg = check_rate(config) if cfg_ok else (False, "跳过（配置未加载）")
    print(("OK  " if rate_ok else "FAIL"), "限速:", rate_msg)
    demo_ok, demo_msg = (False, "跳过")
    if py_ok and cfg_ok and dir_ok:
        bot = config.get("bot") or {}
        data_dir = Path(bot.get("data_dir") or "data")
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        demo_ok, demo_msg = check_demo(root, data_dir / "selfcheck")
    print(("OK  " if demo_ok else "FAIL"), "演示:", demo_msg)

    ok = all([py_ok, dep_ok, cfg_ok, dir_ok, rate_ok, demo_ok])
    print("======== 自检", "通过" if ok else "未通过", "========")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

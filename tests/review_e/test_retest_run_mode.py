# -*- coding: utf-8 -*-
"""复测 4：set_mode 记日志、重启保持；优先级 状态文件>配置>默认 observe；--mode / mode_force 覆盖。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from random import Random

from src.clock import FakeClock
from src.config_util import load_config
from src.main import build_app, parse_args
from src.run_mode import RunMode, SendGate

from .helpers import REPO_ROOT, attach_logger, make_app, make_sender


def _build(
    tmp_path: Path,
    *,
    run: dict | None = None,
    force_mode: str | None = None,
    pop_run: bool = False,
    use_example: bool = False,
):
    if use_example:
        config = load_config(REPO_ROOT / "config" / "example.toml", project_root=REPO_ROOT)
        config = dict(config)
    else:
        config = load_config(REPO_ROOT / "config" / "example.toml", project_root=REPO_ROOT)
        config = dict(config)
        if pop_run:
            config.pop("run", None)
        elif run is not None:
            merged = dict(config.get("run") or {})
            merged.update(run)
            config["run"] = merged
    rate = dict(config.get("rate_limit") or {})
    rate["send_delay_min"] = 0
    rate["send_delay_max"] = 0
    rate["typing_hook"] = False
    night = dict(rate.get("night_silent") or {})
    night["enabled"] = False
    rate["night_silent"] = night
    config["rate_limit"] = rate
    return build_app(
        config,
        project_root=REPO_ROOT,
        clock=FakeClock(datetime(2026, 11, 11, 10, 0, 0)),
        rng=Random(1),
        data_dir=tmp_path,
        force_mode=force_mode,
    )


def test_retest_set_mode_logs_and_persists_after_restart(tmp_path: Path, clock: FakeClock):
    logger, handler = attach_logger("wechat_bot.review_e.retest_mode")
    _, _, _, gate = make_sender(clock, tmp_path, "observe", logger=logger)
    gate.state_path = tmp_path / "run_mode.json"
    gate.set_mode("reply")
    joined = "\n".join(handler.records)
    assert any("reply" in rec.lower() and ("切换" in rec or "模式" in rec) for rec in handler.records), (
        f"set_mode 应记切换日志，实际：{joined!r}"
    )
    assert (tmp_path / "run_mode.json").exists()

    data = tmp_path / "persist"
    app = make_app(data, clock, mode=None)
    app["send_gate"].set_mode("reply")
    restarted = make_app(data, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode=None)
    assert restarted["send_gate"].get_mode() == "reply"
    assert restarted["send_gate"].mode is RunMode.REPLY


def test_retest_state_file_outranks_explicit_config_mode(tmp_path: Path):
    """状态文件优先于配置里的 run.mode：set_mode(reply) 后即使配置仍写 observe，重启保持 reply。"""
    data = tmp_path / "prio"
    app = _build(data, run={"mode": "observe"})
    app["send_gate"].set_mode("reply")
    restarted = _build(data, run={"mode": "observe"})
    assert restarted["send_gate"].get_mode() == "reply", (
        f"状态文件应为最高优先级，实际恢复为 {restarted['send_gate'].get_mode()!r}"
    )
    # 配置高于缺省：无状态文件时，配置 announce 应生效。
    fresh = _build(tmp_path / "from_cfg", run={"mode": "announce"})
    assert fresh["send_gate"].get_mode() == "announce"


def test_retest_missing_state_and_config_defaults_to_observe(tmp_path: Path):
    app = _build(tmp_path, pop_run=True)
    assert app["send_gate"].get_mode() == "observe"
    assert app["run_mode"] is RunMode.OBSERVE


def test_retest_example_config_does_not_clobber_state(tmp_path: Path):
    """示例配置不写生效的 run.mode，重启不得把已切换的 reply 打回 observe。"""
    data = tmp_path / "example"
    app = _build(data, use_example=True)
    assert app["send_gate"].get_mode() == "observe"
    app["send_gate"].set_mode("reply")
    restarted = _build(data, use_example=True)
    assert restarted["send_gate"].get_mode() == "reply", (
        f"示例配置覆盖了状态文件，重启后为 {restarted['send_gate'].get_mode()!r}"
    )


def test_retest_mode_force_and_cli_mode_override_state(tmp_path: Path):
    data = tmp_path / "force"
    app = _build(data, pop_run=True)
    app["send_gate"].set_mode("reply")
    assert app["send_gate"].get_mode() == "reply"

    forced_cfg = _build(data, run={"mode": "observe", "mode_force": True})
    assert forced_cfg["send_gate"].get_mode() == "observe", (
        "run.mode_force=true 应以配置覆盖状态文件"
    )
    # 状态文件应被回写成 observe。
    assert forced_cfg["send_gate"].get_mode() == "observe"

    # 再切回 reply，然后用 --mode / force_mode 覆盖。
    data2 = tmp_path / "cli"
    first = _build(data2, pop_run=True)
    first["send_gate"].set_mode("announce")
    cli = _build(data2, pop_run=True, force_mode="reply")
    assert cli["send_gate"].get_mode() == "reply", (
        f"--mode / force_mode 应覆盖状态，实际 {cli['send_gate'].get_mode()!r}"
    )
    args = parse_args(["--demo", "--mode", "announce"])
    assert args.mode == "announce"


def test_retest_invalid_mode_falls_back_to_observe_and_starts(tmp_path: Path):
    app = _build(tmp_path / "bad", run={"mode": "broadcast"})
    assert app["run_mode"] is RunMode.OBSERVE
    assert app["send_gate"].get_mode() == "observe"
    # 仍可装配发送器，只是不发。
    app["sender"].submit_auto_reply("g1", "不应发出")
    assert app["sender"].pump() == 0


def test_retest_invalid_mode_force_overwrites_existing_state(tmp_path: Path):
    """非法 run.mode 降级后不得覆盖已有合法状态（例如 reply）。"""
    data = tmp_path / "keep_on_bad"
    app = _build(data, pop_run=True)
    app["send_gate"].set_mode("reply")
    restarted = _build(data, run={"mode": "broadcast"})
    assert restarted["send_gate"].get_mode() == "reply", (
        f"非法配置回退 observe 时不得覆盖已有 reply，实际 {restarted['send_gate'].get_mode()!r}"
    )
    assert restarted["run_mode"] is RunMode.REPLY


def test_retest_state_file_invalid_or_unknown_falls_back_to_observe(tmp_path: Path):
    """状态文件误写成非法值或未知值时，应回退 observe，并重写成合法值。"""
    path = tmp_path / "run_mode.json"
    path.write_text('{"run_mode": "broadcast"}', encoding="utf-8")
    gate = SendGate(mode="announce", state_path=path, config_explicit=False)
    assert gate.get_mode() == "observe"
    assert gate.mode_source == "state_corrupt"
    saved = path.read_text(encoding="utf-8")
    assert "observe" in saved
    assert "broadcast" not in saved

    path2 = tmp_path / "unknown.json"
    path2.write_text('{"run_mode": "xyz-unknown"}', encoding="utf-8")
    gate2 = SendGate(mode="reply", state_path=path2, config_explicit=False)
    assert gate2.get_mode() == "observe"
    assert gate2.mode_source == "state_corrupt"
    saved2 = path2.read_text(encoding="utf-8")
    assert "observe" in saved2
    assert "xyz-unknown" not in saved2


def test_retest_startup_logs_effective_mode_and_source(tmp_path: Path, caplog):
    """启动时应打印生效模式及其来源。"""
    with caplog.at_level(logging.INFO, logger="wechat_bot"):
        app = _build(tmp_path / "src_cfg", run={"mode": "announce"})
    assert app["send_gate"].get_mode() == "announce"
    joined = caplog.text
    assert "运行模式生效" in joined
    assert "announce" in joined
    assert "来源" in joined

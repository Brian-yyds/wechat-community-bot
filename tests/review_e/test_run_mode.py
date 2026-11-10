# -*- coding: utf-8 -*-
"""运行模式：observe / announce / reply 在发送出口强制；切换、持久化、非法配置。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.clock import FakeClock
from src.config_util import ConfigError
from src.run_mode import RunMode, RunModeError, SendGate, mode_allows, parse_run_mode

from .helpers import make_app, make_sender, attach_logger


def test_missing_and_empty_mode_default_to_observe():
    assert parse_run_mode(None) is RunMode.OBSERVE
    assert parse_run_mode("") is RunMode.OBSERVE
    assert parse_run_mode("   ") is RunMode.OBSERVE
    assert parse_run_mode("Observe") is RunMode.OBSERVE


def test_example_config_and_build_app_default_observe(tmp_path: Path, clock: FakeClock):
    app = make_app(tmp_path, clock, mode=None)
    assert app["run_mode"] is RunMode.OBSERVE
    assert app["send_gate"].mode is RunMode.OBSERVE


def test_observe_blocks_reply_and_announcement_at_sender_exit(tmp_path: Path, clock: FakeClock):
    sender, adapter, _, _ = make_sender(clock, tmp_path, "observe")
    sender.submit_auto_reply("g1", "自动回复")
    sender.submit_announcement("g1", "定时公告")
    assert sender.pump() == 0
    assert sender.dropped_count == 2
    assert adapter.sent_messages == []


def test_announce_sends_announcement_only(tmp_path: Path, clock: FakeClock):
    sender, adapter, _, _ = make_sender(clock, tmp_path, "announce")
    sender.submit_auto_reply("g1", "自动回复不应发出")
    sender.submit_announcement("g1", "公告应发出")
    assert sender.pump() == 1
    assert [m.text for m in adapter.sent_messages] == ["公告应发出"]


def test_reply_sends_both(tmp_path: Path, clock: FakeClock):
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply")
    sender.submit_auto_reply("g1", "回复")
    sender.submit_announcement("g1", "公告")
    assert sender.pump() == 2
    assert {m.text for m in adapter.sent_messages} == {"回复", "公告"}


def test_bypass_pipeline_and_call_sender_directly_is_still_gated(tmp_path: Path, clock: FakeClock):
    """绕过管线/调度器，直接调用 HumanizedSender 提交，出口仍必须拦截。"""
    sender, adapter, _, _ = make_sender(clock, tmp_path, "observe")
    sender.submit("g1", "直接调用也不应发出", kind="auto_reply")
    sender.submit("g1", "直接调用公告也不应发出", kind="announcement")
    assert sender.pump() == 0
    assert adapter.sent_messages == []


def test_mode_switch_in_memory_takes_effect_immediately(tmp_path: Path, clock: FakeClock):
    sender, adapter, _, gate = make_sender(clock, tmp_path, "observe")
    sender.submit_auto_reply("g1", "切换前")
    sender.pump()
    assert adapter.sent_messages == []

    gate.mode = RunMode.REPLY
    sender.submit_auto_reply("g1", "切换后")
    assert sender.pump() == 1
    assert adapter.sent_messages[-1].text == "切换后"

    gate.mode = RunMode.ANNOUNCE
    sender.submit_auto_reply("g1", "announce 下的回复")
    sender.submit_announcement("g1", "announce 下的公告")
    sender.pump()
    texts = [m.text for m in adapter.sent_messages]
    assert "announce 下的公告" in texts
    assert "announce 下的回复" not in texts


def test_mode_from_config_survives_rebuild(tmp_path: Path):
    """同一配置重建（模拟重启）应保持 run.mode。"""
    first = make_app(tmp_path / "a", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="announce")
    assert first["run_mode"] is RunMode.ANNOUNCE
    second = make_app(tmp_path / "b", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="announce")
    assert second["run_mode"] is RunMode.ANNOUNCE
    assert mode_allows(second["run_mode"], "announcement")
    assert not mode_allows(second["run_mode"], "auto_reply")


def test_set_mode_persists_when_config_has_no_explicit_mode(tmp_path: Path):
    """配置未写 run.mode 时，set_mode 应落盘并在重建后恢复。"""
    data = tmp_path / "persist"
    clock = FakeClock(datetime(2026, 11, 11, 10, 0, 0))
    app = make_app(data, clock, mode=None)
    assert hasattr(app["send_gate"], "set_mode")
    app["send_gate"].set_mode("reply")
    assert app["send_gate"].get_mode() == "reply"
    assert (data / "run_mode.json").exists()

    restarted = make_app(data, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode=None)
    assert restarted["send_gate"].get_mode() == "reply"


def test_explicit_config_mode_overrides_and_clobbers_state_on_rebuild(tmp_path: Path):
    """示例配置显式写了 run.mode 时，重启以配置为准，会覆盖 set_mode 写入的状态文件。"""
    data = tmp_path / "clobber"
    app = make_app(data, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="observe")
    app["send_gate"].set_mode("reply")
    assert app["send_gate"].get_mode() == "reply"
    restarted = make_app(data, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="observe")
    # 验收「重启后保持」在这种默认用法下不成立：config_explicit=True 会用 observe 并回写状态。
    assert restarted["send_gate"].get_mode() == "reply", (
        "运行时 set_mode('reply') 后，即使配置仍写 observe，重启也应保持 reply；"
        f"实际恢复为 {restarted['send_gate'].get_mode()!r}。"
        "当前 build_app 在配置显式包含 run.mode 时 config_explicit=True，"
        "会忽略并覆盖 data/run_mode.json。"
    )


def test_no_hot_reload_of_config_file(tmp_path: Path, clock: FakeClock):
    """改配置对象/文件后，已装配的 SendGate 不会自动重读；应通过 set_mode 即时切换。"""
    app = make_app(tmp_path, clock, mode="observe")
    assert app["send_gate"].get_mode() == "observe"
    assert hasattr(app["send_gate"], "set_mode")
    assert not hasattr(app["send_gate"], "reload")


def test_mode_switch_should_be_logged(tmp_path: Path, clock: FakeClock):
    logger, handler = attach_logger("wechat_bot.review_e.mode")
    sender, _, _, gate = make_sender(clock, tmp_path, "observe", logger=logger)
    if hasattr(gate, "set_mode"):
        gate.set_mode("reply")
    else:
        gate.mode = RunMode.REPLY
    joined = "\n".join(handler.records)
    assert any(
        "reply" in rec.lower() and ("切换" in rec or "模式" in rec or "set_mode" in rec.lower())
        for rec in handler.records
    ), f"set_mode/模式切换应记日志，实际日志为：{joined!r}"


def test_invalid_mode_should_fall_back_to_observe_not_reply(tmp_path: Path, clock: FakeClock):
    """验收：非法配置默认安全，应落到 observe，而不是 reply，也不应无法启动观察。"""
    try:
        app = make_app(tmp_path, clock, mode="broadcast")
    except (RunModeError, ConfigError) as exc:
        pytest.fail(
            "非法 run.mode 应降级为 observe 并继续只读运行，"
            f"当前直接抛出 {type(exc).__name__}: {exc}"
        )
    assert app["run_mode"] is RunMode.OBSERVE
    assert app["send_gate"].mode is RunMode.OBSERVE


def test_invalid_mode_does_not_silently_become_reply():
    """即便不降级到 observe，也绝不能静默落到 reply。"""
    with pytest.raises((RunModeError, ConfigError, ValueError)):
        parse_run_mode("broadcast")
    with pytest.raises((RunModeError, ValueError)):
        SendGate(mode="not-a-mode")


def test_end_to_end_modes_via_pipeline_and_scheduler(tmp_path: Path):
    observe = make_app(tmp_path / "obs", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="observe")
    observe["adapter"].start()
    observe["adapter"].inject_message("你好，在吗？", sender_id="u1")
    anns = observe["scheduler"].list_announcements()
    if anns:
        observe["scheduler"].fire_now(anns[0].id)
    observe["sender"].pump()
    observe["adapter"].stop()
    assert observe["adapter"].sent_messages == []

    announce = make_app(tmp_path / "ann", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="announce")
    announce["adapter"].start()
    announce["adapter"].inject_message("你好，在吗？", sender_id="u1")
    announce["scheduler"].fire_now(announce["scheduler"].list_announcements()[0].id)
    announce["sender"].pump()
    announce["adapter"].stop()
    texts = [m.text for m in announce["adapter"].sent_messages]
    assert texts and all("您好，我是群助手" not in t for t in texts)
    assert any("早上好" in t or "风险" in t for t in texts)

    reply = make_app(tmp_path / "rep", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="reply")
    reply["adapter"].start()
    reply["adapter"].inject_message("你好，在吗？", sender_id="u1")
    reply["scheduler"].fire_now(reply["scheduler"].list_announcements()[0].id)
    reply["sender"].pump()
    reply["adapter"].stop()
    texts = [m.text for m in reply["adapter"].sent_messages]
    assert any("您好，我是群助手" in t for t in texts)
    assert any("早上好" in t or "风险" in t for t in texts)


def test_observe_still_receives_and_records_messages(tmp_path: Path):
    """observe 只收不发，但应继续处理/落盘消息。"""
    app = make_app(tmp_path, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="observe")
    app["adapter"].start()
    app["adapter"].inject_message("你好，在吗？", sender_id="u1")
    app["sender"].pump()
    app["adapter"].stop()
    assert app["results"], "observe 模式应仍然整理识别入站消息"
    assert app["adapter"].sent_messages == []

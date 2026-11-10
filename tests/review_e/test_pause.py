# -*- coding: utf-8 -*-
"""紧急暂停：PAUSE 存在即拒绝所有发送；删除后恢复；竞态；读取失败默认安全。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from src.adapters.pause import PauseGate, SendPausedError
from src.clock import FakeClock
from src.ratelimit.sender import HumanizeConfig, NightSilentConfig
from src.run_mode import SendGate

from .helpers import make_sender


def test_pause_blocks_reply_and_announcement(tmp_path: Path, clock: FakeClock):
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply", paused=True)
    assert gate.is_paused()
    assert (tmp_path / "PAUSE").exists()
    sender.submit_auto_reply("g1", "回复")
    sender.submit_announcement("g1", "公告")
    assert sender.pump() == 0
    assert adapter.sent_messages == []
    assert sender.dropped_count == 2


def test_pause_content_is_ignored(tmp_path: Path, clock: FakeClock):
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply")
    (tmp_path / "PAUSE").write_text("resume=true\nnot-paused\n", encoding="utf-8")
    sender.submit_auto_reply("g1", "不应发出")
    sender.pump()
    assert adapter.sent_messages == []


def test_resume_after_deleting_pause_file(tmp_path: Path, clock: FakeClock):
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply", paused=True)
    sender.submit_auto_reply("g1", "暂停中")
    sender.pump()
    assert adapter.sent_messages == []

    gate.resume()
    assert not gate.is_paused()
    sender.submit_auto_reply("g1", "恢复后")
    assert sender.pump() == 1
    assert adapter.sent_messages[0].text == "恢复后"


def test_pause_created_during_humanize_delay_is_honored(tmp_path: Path, clock: FakeClock):
    humanize = HumanizeConfig(
        send_delay_min=2.0,
        send_delay_max=2.0,
        typing_hook=False,
        night=NightSilentConfig(enabled=False),
    )
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply", humanize=humanize)
    sender.submit_auto_reply("g1", "延迟期间暂停")
    gate.pause("中途")
    assert sender.pump() == 0
    assert adapter.sent_messages == []


def test_pause_gate_check_raises(tmp_path: Path):
    gate = PauseGate(data_dir=tmp_path)
    gate.pause("unit")
    with pytest.raises(SendPausedError):
        gate.check()
    gate.resume()
    gate.check()  # 不应抛


def test_concurrent_resume_must_not_raise(tmp_path: Path):
    """多个线程同时 resume 时，不应因 exists+unlink 竞态抛出 FileNotFoundError。"""
    gate = PauseGate(data_dir=tmp_path)
    gate.pause("concurrent")
    errors: list[BaseException] = []

    def _resume() -> None:
        try:
            gate.resume()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_resume) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f"并发 resume 抛出异常: {errors!r}"
    assert not (tmp_path / "PAUSE").exists()


def test_concurrent_sends_respect_pause_flag(tmp_path: Path, clock: FakeClock):
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply")
    for i in range(8):
        sender.submit_auto_reply("g1", f"m{i}")
    gate.pause("并发暂停")
    sent = sender.pump()
    assert sent == 0
    assert adapter.sent_messages == []


def test_pause_read_failure_must_default_to_blocked(tmp_path: Path, clock: FakeClock, monkeypatch):
    """读取失败时必须默认安全（拒绝发送），不能当作「未暂停」，也不能把异常抛出发送循环。"""
    sender, adapter, gate, send_gate = make_sender(clock, tmp_path, "reply")

    def _boom(self):
        raise OSError("stat failed")

    monkeypatch.setattr(type(gate.path), "exists", _boom)

    sender.submit_auto_reply("g1", "读取失败也不应发出")
    try:
        sender.pump()
    except OSError:
        sent = list(adapter.sent_messages)
        if sent:
            pytest.fail(f"PAUSE 读取失败时已发出消息: {sent}")
        pytest.fail(
            "PAUSE 读取失败时 decide/is_paused 直接抛 OSError，"
            "发送循环可能中断，且没有默认拒绝发送的安全分支。"
        )
    assert adapter.sent_messages == [], (
        f"PAUSE 读取失败时默认应拒绝发送，实际发出 {adapter.sent_messages}"
    )


def test_pause_exists_stat_denied_must_not_fail_open(tmp_path: Path):
    """父目录不可访问时，Path.exists() 可能返回 False，必须按「已暂停」处理。"""
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    pause = hidden / "PAUSE"
    pause.write_text("paused\n", encoding="utf-8")
    gate = PauseGate(path=pause)
    assert gate.is_paused() is True

    os.chmod(hidden, 0o000)
    result = None
    raised = None
    decision = None
    try:
        try:
            result = gate.is_paused()
        except OSError as exc:
            raised = exc
            result = None
        send_gate = SendGate(mode="reply", pause_gate=gate)
        try:
            decision = send_gate.decide("auto_reply")
        except OSError as exc:
            raised = exc
            decision = None
    finally:
        os.chmod(hidden, 0o700)

    if raised is not None:
        pytest.fail(
            f"无法 stat PAUSE 时 is_paused/decide 抛出 {type(raised).__name__}: {raised}，"
            "应默认视为已暂停并拒绝发送，而不是把异常抛出发送出口。"
        )
    assert result is True, (
        f"无法 stat PAUSE 文件时 is_paused 应为 True（默认安全），实际为 {result}"
    )
    assert decision is not None and decision.allowed is False, (
        f"无法 stat PAUSE 文件时必须拒绝发送，实际 decision={decision}"
    )

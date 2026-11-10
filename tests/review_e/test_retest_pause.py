# -*- coding: utf-8 -*-
"""复测 1：PAUSE 读取失败默认拒发且不抛进发送循环；resume 无 exists+unlink 竞态。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from src.adapters.pause import PauseGate, SendPausedError
from src.clock import FakeClock
from src.run_mode import SendGate

from .helpers import make_sender


def test_retest_pause_read_failure_defaults_to_blocked_and_does_not_raise(
    tmp_path: Path, clock: FakeClock, monkeypatch
):
    sender, adapter, gate, _ = make_sender(clock, tmp_path, "reply")

    def _boom(self):
        raise OSError("stat failed")

    monkeypatch.setattr(type(gate.path), "exists", _boom)

    sender.submit_auto_reply("g1", "读取失败也不应发出")
    sender.submit_announcement("g1", "公告也不应发出")
    try:
        sent = sender.pump()
    except OSError as exc:
        pytest.fail(f"PAUSE 读取失败把异常抛进了发送循环: {exc}")
    assert sent == 0
    assert adapter.sent_messages == []
    assert sender.dropped_count >= 1
    assert gate.is_paused() is True
    decision = SendGate(mode="reply", pause_gate=gate).decide("auto_reply")
    assert decision.allowed is False and decision.paused is True


def test_retest_pause_stat_denied_defaults_to_blocked(tmp_path: Path):
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    pause = hidden / "PAUSE"
    pause.write_text("paused\n", encoding="utf-8")
    gate = PauseGate(path=pause)
    os.chmod(hidden, 0o000)
    raised = None
    result = None
    decision = None
    try:
        try:
            result = gate.is_paused()
            decision = SendGate(mode="reply", pause_gate=gate).decide("auto_reply")
        except OSError as exc:
            raised = exc
    finally:
        os.chmod(hidden, 0o700)
    assert raised is None, f"无法 stat 时不得抛出 {raised!r}"
    assert result is True
    assert decision is not None and decision.allowed is False and decision.paused is True


def test_retest_sendgate_is_paused_swallows_oserror_and_denies(tmp_path: Path, monkeypatch):
    """即便底层 is_paused 仍抛 OSError，SendGate 也应默认拒绝且不往外抛。"""
    gate = PauseGate(data_dir=tmp_path)

    def _boom(_self=gate):
        raise PermissionError("cannot stat")

    monkeypatch.setattr(gate, "is_paused", _boom)
    send_gate = SendGate(mode="reply", pause_gate=gate)
    try:
        paused = send_gate.is_paused()
        decision = send_gate.decide("auto_reply")
    except OSError as exc:
        pytest.fail(f"SendGate.is_paused/decide 把读取失败抛出去了: {exc}")
    assert paused is True
    assert decision.allowed is False and decision.paused is True


def test_retest_resume_missing_file_is_false_not_error(tmp_path: Path):
    gate = PauseGate(data_dir=tmp_path)
    assert gate.resume() is False
    assert not (tmp_path / "PAUSE").exists()


def test_retest_concurrent_resume_has_no_exists_unlink_race(tmp_path: Path):
    """反复创建 PAUSE 后由多线程同时 resume，不应因 exists+unlink 抛 FileNotFoundError。"""
    gate = PauseGate(data_dir=tmp_path)
    errors: list[BaseException] = []

    for _ in range(50):
        gate.pause("race")
        start = threading.Event()

        def _resume() -> None:
            start.wait(timeout=5)
            try:
                gate.resume()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_resume) for _ in range(24)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=5)
            assert not t.is_alive(), "并发 resume 线程超时"
        assert not (tmp_path / "PAUSE").exists()
    assert not errors, f"并发 resume 抛出异常: {errors!r}"


def test_retest_check_still_raises_when_paused(tmp_path: Path):
    gate = PauseGate(data_dir=tmp_path)
    gate.pause("unit")
    with pytest.raises(SendPausedError):
        gate.check()
    gate.resume()
    gate.check()

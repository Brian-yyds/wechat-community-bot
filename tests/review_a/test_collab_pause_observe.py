# -*- coding: utf-8 -*-
"""与其他模块协同：PAUSE 存在时仍只读抓取；observe 模式只读不发。

紧急暂停文件一律建在 tmp_path 并通过环境变量隔离，绝不触碰仓库 data/PAUSE。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.adapters.base import EventType
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.local_capture import LocalCaptureAdapter
from src.adapters.mock import MockAdapter
from src.adapters.pause import PauseGate
from src.clock import FakeClock
from src.run_mode import RunMode, SendGate

from review_a.helpers import SHANGHAI, make_db, pause_isolated


def test_pause_file_does_not_stop_local_capture(tmp_path: Path, monkeypatch):
    pause_path = pause_isolated(tmp_path, monkeypatch)
    pause_path.write_text("paused_at=2026-11-11T03:00:00\nreason=review-a\n", encoding="utf-8")
    gate = PauseGate(path=pause_path)
    assert gate.is_paused() is True

    db = make_db(tmp_path)
    db.insert(msg_id="p-1", group_id="g1", sender_id="u", content="暂停期间仍应抓取", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    adapter = LocalCaptureAdapter(db.path, cursor_store={})
    got = adapter.poll_once()
    assert [m.message_id for m in got] == ["p-1"], "PAUSE 是发送闸门，不应阻断适配器 A 的只读抓取"
    adapter.reader.close()
    # 确认没有误删隔离出的暂停文件，更没有去动仓库 data/PAUSE
    assert pause_path.is_file()
    repo_pause = Path(__file__).resolve().parents[2] / "data" / "PAUSE"
    # 不创建也不删除仓库开关；此处只断言测试没用仓库路径
    assert pause_path != repo_pause


def test_observe_mode_captures_but_send_gate_drops(tmp_path: Path, monkeypatch):
    pause_path = pause_isolated(tmp_path, monkeypatch)
    # 不创建 PAUSE，仅隔离路径，避免误用仓库文件
    assert not pause_path.exists()

    db = make_db(tmp_path)
    db.insert(msg_id="o-1", group_id="g1", sender_id="u1", content="观察期提问", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    clock = FakeClock(datetime(2026, 11, 11, 10, 0, 0))
    inbound = LocalCaptureAdapter(db.path, cursor_store=tmp_path / "cursor.json", clock=clock)
    outbound = MockAdapter(clock=clock)
    composite = CompositeAdapter(inbound, outbound)
    received = []
    composite.subscribe(received.append)
    messages = inbound.poll_once()
    assert [m.message_id for m in messages] == ["o-1"]
    assert any(e.event_type == EventType.MESSAGE for e in received)

    gate = SendGate(mode=RunMode.OBSERVE, pause_gate=PauseGate(path=pause_path))
    decision = gate.decide("auto_reply")
    assert decision.allowed is False
    assert decision.mode == "observe"
    # observe 下不得真正发出
    if decision.allowed:
        outbound.send_text("g1", "这是误发")
    assert outbound._sent == []
    inbound.reader.close()


def test_pause_blocks_send_but_not_capture(tmp_path: Path, monkeypatch):
    pause_path = pause_isolated(tmp_path, monkeypatch)
    PauseGate(path=pause_path).pause("人工实测")
    db = make_db(tmp_path)
    db.insert(msg_id="z-1", group_id="g1", sender_id="u", content="抓取", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    inbound = LocalCaptureAdapter(db.path, cursor_store={})
    assert [m.message_id for m in inbound.poll_once()] == ["z-1"]
    gate = SendGate(mode=RunMode.REPLY, pause_gate=PauseGate(path=pause_path))
    d = gate.decide("auto_reply")
    assert d.allowed is False and d.paused is True
    inbound.reader.close()
    assert pause_path.is_file()


def test_factory_local_capture_plus_mock_is_inbound_only_for_observe(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="f-1", group_id="g1", sender_id="u", content="工厂装配", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    clock = FakeClock(datetime(2026, 11, 11, 10, 0, 0))
    adapter = build_adapter(
        {
            "wechat": {"inbound": "local_capture", "outbound": "mock"},
            "bot": {"data_dir": str(tmp_path / "data"), "identity": {"self_ids": []}},
            "inbound": {"local_capture": {"db_path": str(db.path), "cursor_path": str(tmp_path / "data" / "cursor.json")}},
        },
        clock=clock,
    )
    assert isinstance(adapter, CompositeAdapter)
    assert isinstance(adapter.inbound, LocalCaptureAdapter)
    assert isinstance(adapter.outbound, MockAdapter)
    got = adapter.inbound.poll_once()
    assert [m.message_id for m in got] == ["f-1"]
    # 适配器 A 本身没有发送方法；发送由 outbound 执行。observe 下只要不调用 outbound 就不会发。
    assert not hasattr(adapter.inbound, "send_text") or not callable(getattr(adapter.inbound, "send_text", None))
    assert adapter.outbound._sent == []
    adapter.inbound.reader.close()

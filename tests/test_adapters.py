"""协议适配层：抽象接口与 MockAdapter。"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from src.adapters.base import EventType, Group
from src.adapters.mock import MockAdapter
from src.clock import FakeClock


def test_start_stop_and_lifecycle_events(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    events = []
    adapter.subscribe(events.append)
    adapter.start()
    adapter.start()  # 幂等
    assert adapter.running is True
    adapter.stop()
    adapter.stop()
    types = [e.event_type for e in events]
    assert types == [EventType.ADAPTER_STARTED, EventType.ADAPTER_STOPPED]


def test_inject_before_start_is_pending_then_flushed(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    received = []
    adapter.subscribe(lambda e: received.append(e))
    msg = adapter.inject_message("hello")
    assert msg.content == "hello"
    assert received == []
    adapter.start()
    messages = [e.message for e in received if e.message]
    assert len(messages) == 1
    assert messages[0].content == "hello"
    assert adapter.event_queue().qsize() >= 2  # started + message


def test_inject_after_start_dispatches_immediately(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    received = []
    adapter.subscribe(received.append)
    adapter.start()
    adapter.inject_message("ping", sender_id="u1", sender_name="张三")
    msgs = [e.message for e in received if e.message]
    assert msgs[0].sender_id == "u1"
    assert msgs[0].message_id.startswith("mock-")


def test_send_text_is_recorded_and_list_groups():
    groups = [Group("g1", "一群"), Group("g2", "二群")]
    adapter = MockAdapter(groups=groups)
    adapter.send_text("g1", "早上好")
    assert len(adapter.sent_messages) == 1
    assert adapter.sent_messages[0].group_id == "g1"
    assert adapter.sent_messages[0].text == "早上好"
    assert [g.group_id for g in adapter.list_groups()] == ["g1", "g2"]


def test_notify_typing_hook(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    adapter.notify_typing("g1")
    assert adapter.typing_events[0][0] == "g1"


def test_inject_from_file_jsonl_and_array(tmp_path: Path, clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    adapter.start()
    jsonl = tmp_path / "a.jsonl"
    jsonl.write_text(
        json.dumps({"content": "第一条", "group_id": "demo-group"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"content": "第二条", "sender_id": "u2"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    msgs = adapter.inject_from_file(jsonl)
    assert [m.content for m in msgs] == ["第一条", "第二条"]

    arr = tmp_path / "b.json"
    arr.write_text(json.dumps([{"content": "数组消息"}], ensure_ascii=False), encoding="utf-8")
    msgs2 = adapter.inject_from_file(arr)
    assert msgs2[0].content == "数组消息"


def test_inject_from_stdin_jsonl_and_plain_text(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    adapter.start()
    stream = StringIO('{"content": "来自json"}\n{"content": "第二行"}\n')
    msgs = adapter.inject_from_stdin(stream)
    assert [m.content for m in msgs] == ["来自json", "第二行"]

    adapter2 = MockAdapter(clock=clock)
    adapter2.start()
    plain = StringIO("纯文本一行\n另一行\n")
    msgs2 = adapter2.inject_from_stdin(plain)
    assert [m.content for m in msgs2] == ["纯文本一行", "另一行"]


def test_unsubscribe_and_event_queue(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    bucket = []
    cancel = adapter.subscribe(bucket.append)
    adapter.start()
    cancel()
    adapter.inject_message("之后不应再回调")
    # 生命周期 started 可能已进入 bucket，但注入后不应再增加消息回调
    before = len(bucket)
    adapter.inject_message("再来一条")
    assert len(bucket) == before
    # 队列仍能收到
    kinds = []
    while not adapter.event_queue().empty():
        kinds.append(adapter.event_queue().get_nowait().event_type)
    assert EventType.MESSAGE in kinds

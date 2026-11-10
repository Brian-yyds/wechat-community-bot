"""C 方案：通知 / 会话列表预览旁路的单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime

import pytest

from src.adapters.base import EventType, Group
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.mock import MockAdapter
from src.adapters.notification import (
    FakeNotificationSource,
    NotificationAdapter,
    NotificationAssist,
    NotificationHint,
    WindowsNotificationSource,
)
from src.adapters.roles import INBOUND_NOTIFICATION, IMPLEMENTED_INBOUND
from src.clock import FakeClock


def _hints():
    return [
        {"group": "交易会员群-主群", "preview": "有人提问手续费", "ts": "2026-11-11T12:00:00+08:00", "sender": "张三"},
        {"group": "免打扰群", "preview": "被抑制的通知", "ts": "2026-11-11T12:00:01+08:00"},
    ]


def test_module_imports_on_linux():
    """本模块在 Linux 上必须能 import，不得在模块顶层拉 Windows 依赖。"""
    assert "src.adapters.notification" in sys.modules
    assert sys.platform != "win32" or True  # 在 box 上通常不是 win32，但两边都不应崩溃。


def test_windows_source_refuses_linux():
    with pytest.raises(RuntimeError, match="只能在 Windows 上使用"):
        WindowsNotificationSource()


def test_not_a_complete_source():
    assist = NotificationAssist(hints=_hints(), suppressed={"免打扰群"})
    assert assist.is_complete_source() is False
    assert assist.completeness_guaranteed() is False
    assert NotificationAdapter is NotificationAssist


def test_poll_hints_filters_suppressed_and_marks_incomplete(clock: FakeClock):
    assist = NotificationAdapter(
        hints=_hints(),
        suppressed={"免打扰群"},
        clock=clock,
    )
    hints = assist.poll_hints()
    groups = [h.group for h in hints]
    assert "交易会员群-主群" in groups
    assert "免打扰群" not in groups
    assert all(isinstance(h, NotificationHint) for h in hints)
    assert all(h.complete is False for h in hints)
    assert all(h.partial is True for h in hints)


def test_empty_hints_are_not_zero_message_proof(clock: FakeClock):
    assist = NotificationAdapter(hints=[], suppressed=set(), clock=clock)
    assert assist.poll_hints() == []
    # 没有实现 has_no_messages；即便以后加上，也不得返回 True。
    if hasattr(assist, "has_no_messages"):
        assert assist.has_no_messages("任意群") is not True


def test_preview_events_are_marked_partial(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    source.push("交易会员群-主群", "请问开户要哪些资料", sender="李四", group_id="g-main")
    adapter = NotificationAdapter(source=source, clock=clock, groups=[Group("g-main", "交易会员群-主群")])
    received = []
    adapter.subscribe(received.append)
    hints = adapter.poll_once()
    assert len(hints) == 1
    messages = [e.message for e in received if e.message]
    assert len(messages) == 1
    msg = messages[0]
    assert msg.content == "请问开户要哪些资料"
    assert msg.extra.get("partial") is True
    assert msg.extra.get("complete") is False
    assert msg.extra.get("source") == "notification"
    assert msg.sender_name == "李四"
    assert received[0].extra.get("partial") is True


def test_wake_callback_is_invoked(clock: FakeClock):
    woken = []
    source = FakeNotificationSource(clock=clock)
    source.push("交易会员群-主群", "有新消息预览", group_id="g1")
    adapter = NotificationAdapter(
        source=source,
        clock=clock,
        wake_callback=lambda hint: woken.append(hint),
    )
    adapter.poll_once()
    assert len(woken) == 1
    assert woken[0].group == "交易会员群-主群"


def test_set_wake_callback_can_be_wired_later(clock: FakeClock):
    woken = []
    adapter = NotificationAdapter(clock=clock)
    adapter.set_wake_callback(lambda hint: woken.append(hint.group))
    adapter.source.push("A群", "hello")  # type: ignore[attr-defined]
    adapter.poll_once()
    assert woken == ["A群"]


def test_composite_wires_wake_to_target(clock: FakeClock):
    class FakeCapture:
        def __init__(self) -> None:
            self.wakes = []

        def request_immediate_poll(self, hint) -> None:
            self.wakes.append(getattr(hint, "group", hint))

        # 最小 Inbound 外观，供 start/stop 不报错（本测试不经过这些方法）。

    capture = FakeCapture()
    source = FakeNotificationSource(clock=clock)
    source.push("交易会员群-主群", "催促轮询")
    inbound = NotificationAdapter(source=source, clock=clock)
    outbound = MockAdapter(clock=clock)
    combo = CompositeAdapter(inbound, outbound, wake_target=capture)
    inbound.poll_once()
    assert capture.wakes == ["交易会员群-主群"]


def test_duplicate_preview_is_not_emitted_twice(clock: FakeClock):
    source = FakeNotificationSource(
        hints=[{"group": "G", "preview": "同一条", "ts": "2026-11-11T12:00:00"}],
        clock=clock,
    )
    adapter = NotificationAdapter(source=source, clock=clock)
    first = adapter.poll_once()
    # 再推同一条（相同 group/preview/ts）
    source.push("G", "同一条", ts="2026-11-11T12:00:00")
    second = adapter.poll_once()
    assert len(first) == 1
    assert second == []


def test_role_is_implemented_and_factory_builds_it(clock: FakeClock):
    assert INBOUND_NOTIFICATION in IMPLEMENTED_INBOUND
    adapter = build_adapter(
        {"wechat": {"inbound": "notification", "outbound": "mock"}},
        groups=[Group("g1", "演示群")],
        clock=clock,
    )
    assert isinstance(adapter, CompositeAdapter)
    assert isinstance(adapter.inbound, NotificationAdapter)
    assert adapter.inbound.is_complete_source() is False

def test_wake_a_on_preview_false_does_not_auto_wire(clock: FakeClock):
    adapter = build_adapter(
        {
            "wechat": {"inbound": "mock", "outbound": "mock"},
            "inbound": {
                "notification": {
                    "enabled": True,
                    "source": "fake",
                    "wake_a_on_preview": False,
                }
            },
        },
        groups=[Group("g1", "演示群")],
        clock=clock,
    )
    assert isinstance(adapter, CompositeAdapter)
    assert adapter.notification is not None
    assert adapter.notification.wake_callback is None
    assert adapter.wake_target is None


def test_wake_a_on_preview_true_wires_by_default(clock: FakeClock):
    class Capture(MockAdapter):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.wakes = []

        def request_immediate_poll(self, hint) -> None:
            self.wakes.append(getattr(hint, "group", hint))

    # 用已构造的 inbound 不易走 factory；这里验证默认配置会设置 wake_target。
    adapter = build_adapter(
        {
            "wechat": {"inbound": "mock", "outbound": "mock"},
            "inbound": {
                "notification": {
                    "enabled": True,
                    "source": "fake",
                    "wake_a_on_preview": True,
                }
            },
        },
        groups=[Group("g1", "演示群")],
        clock=clock,
    )
    assert isinstance(adapter, CompositeAdapter)
    assert adapter.wake_target is adapter.inbound
    assert adapter.notification is not None
    assert adapter.notification.wake_callback is not None


def test_wake_budget_and_merge_by_group(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    woken = []
    adapter = NotificationAdapter(
        source=source,
        clock=clock,
        wake_callback=lambda h: woken.append(h.group),
        wake_budget_per_poll=2,
        merge_wake_by_group=True,
    )
    from datetime import timedelta

    base = datetime(2026, 11, 11, 13, 0, 0)
    # 同一群两条 + 另外两群，共 4 条预览，唤醒应合并为每群一次且最多 2 次。
    source.push("A群", "预览1", ts=base)
    source.push("A群", "预览2", ts=base + timedelta(seconds=1))
    source.push("B群", "预览3", ts=base + timedelta(seconds=2))
    source.push("C群", "预览4", ts=base + timedelta(seconds=3))
    hints = adapter.poll_once()
    assert len(hints) == 4
    assert all(h.partial is True for h in hints)
    assert woken == ["A群", "B群"]


def test_pipeline_does_not_auto_reply_to_partial_preview(clock: FakeClock):
    from src.adapters.base import Message
    from src.pipeline.pipeline import Pipeline

    class Recorder:
        def __init__(self) -> None:
            self.sent = []

        def submit_auto_reply(self, group_id, text, **kwargs) -> None:
            self.sent.append((group_id, text))

    sender = Recorder()
    pipe = Pipeline(sender=sender, clock=clock)
    msg = Message(
        message_id="notify-1",
        group_id="g1",
        sender_id="u1",
        content="你好",
        timestamp=clock.now(),
        sender_name="演示用户",
        extra={"partial": True, "source": "notification", "complete": False},
    )
    result = pipe.process(msg)
    assert result.skipped is True
    assert result.skip_reason == "partial_preview"
    assert result.reply_text is None
    assert sender.sent == []

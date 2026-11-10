# -*- coding: utf-8 -*-
"""C 方案补充用例：去重、乱序、截断、缺群名、洪泛，以及与本地库读取的协同。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.adapters.base import EventType, Group
from src.adapters.composite import CompositeAdapter
from src.adapters.mock import MockAdapter
from src.adapters.notification import (
    FakeNotificationSource,
    NotificationAdapter,
    NotificationHint,
)
from src.clock import FakeClock


def _adapter(clock: FakeClock, **kwargs) -> NotificationAdapter:
    source = kwargs.pop("source", None) or FakeNotificationSource(clock=clock)
    return NotificationAdapter(source=source, clock=clock, **kwargs)


def test_duplicate_notification_is_deduped(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    ts = "2026-11-11T12:00:00"
    source.push("交易会员群-主群", "请问开户要哪些资料", ts=ts, sender="甲")
    adapter = _adapter(clock, source=source)
    first = adapter.poll_once()
    source.push("交易会员群-主群", "请问开户要哪些资料", ts=ts, sender="甲")
    second = adapter.poll_once()
    assert len(first) == 1
    assert second == []


def test_out_of_order_distinct_hints_are_kept_and_late_duplicate_dropped(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    later = datetime(2026, 11, 11, 12, 0, 5)
    earlier = datetime(2026, 11, 11, 12, 0, 1)
    # 先到较新的提示，再到更早的另一条，两者都应保留。
    source.push("交易会员群-主群", "后到的行情讨论", ts=later, sender="乙")
    source.push("交易会员群-主群", "先发出的提问", ts=earlier, sender="甲")
    adapter = _adapter(clock, source=source)
    first = adapter.poll_once()
    previews = [h.preview for h in first]
    assert "后到的行情讨论" in previews
    assert "先发出的提问" in previews
    assert all(h.complete is False and h.partial is True for h in first)

    # 乱序重放较早那条，必须去重。
    source.push("交易会员群-主群", "先发出的提问", ts=earlier, sender="甲")
    assert adapter.poll_once() == []


def test_folded_or_truncated_preview_is_marked_partial(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    truncated = "张三: 请问开户要哪些资料，另外晚上的复盘…"
    source.push("交易会员群-主群", truncated, ts="2026-11-11T12:01:00", sender="张三")
    adapter = _adapter(clock, source=source)
    hints = adapter.poll_once()
    assert len(hints) == 1
    assert hints[0].partial is True
    assert hints[0].complete is False
    assert hints[0].preview == truncated

    # 随后到达更完整的预览：不得被标成完整消息；当前实现按 (group, preview, ts) 去重，
    # 截断与全文视为两条提示，只作唤醒可以接受，但不能变成完整消息源。
    source.push(
        "交易会员群-主群",
        "张三: 请问开户要哪些资料，另外晚上的复盘交流还开吗？",
        ts="2026-11-11T12:01:01",
        sender="张三",
    )
    more = adapter.poll_once()
    assert len(more) == 1
    assert more[0].complete is False
    assert more[0].partial is True


def test_missing_group_name_does_not_invent_group_or_claim_complete(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    source.push("", "有人提问手续费", ts="2026-11-11T12:02:00", sender="丙")
    adapter = _adapter(clock, source=source, groups=[])
    hints = adapter.poll_once()
    assert len(hints) == 1
    assert hints[0].group == ""
    assert hints[0].complete is False
    assert hints[0].partial is True
    # 不得把空群名登记成真实群。
    assert adapter.list_groups() == []


def test_notification_flood_does_not_crash_and_hints_stay_incomplete(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    woken: list[str] = []
    adapter = _adapter(clock, source=source, wake_callback=lambda h: woken.append(h.group))
    base = datetime(2026, 11, 11, 12, 3, 0)
    n = 200
    for i in range(n):
        source.push(
            f"群{i}",
            f"预览{i}",
            ts=base + timedelta(seconds=i),
            sender="丁",
        )
    hints = adapter.poll_once()
    assert len(hints) == n
    assert len(woken) == n
    assert all(h.complete is False and h.partial is True for h in hints)
    # 当前实现没有洪泛节流或同轮合并接口，本用例只确认不崩溃且不把提示标成完整消息。
    # 缺乏节流会在报告中作为中等问题退回（见 REPORT_CD.md）。
    assert not hasattr(adapter, "max_hints_per_poll")
    assert not hasattr(adapter, "coalesce_hints")


def test_assist_mode_does_not_inject_preview_into_composite_inbound_queue(clock: FakeClock):
    """C 作为旁路时，预览不得进入主读取事件队列，只能唤醒 A。"""

    class FakeCapture(MockAdapter):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.wakes: list[str] = []

        def request_immediate_poll(self, hint) -> None:
            self.wakes.append(getattr(hint, "group", str(hint)))

    capture = FakeCapture(clock=clock, groups=[Group("g-main", "交易会员群-主群")])
    source = FakeNotificationSource(clock=clock)
    source.push("交易会员群-主群", "催促轮询的预览", ts="2026-11-11T12:04:00")
    inbound_notify = NotificationAdapter(source=source, clock=clock)
    combo = CompositeAdapter(
        capture,
        MockAdapter(clock=clock),
        wake_target=capture,
        notification=inbound_notify,
    )
    inbound_notify.poll_once()
    assert capture.wakes == ["交易会员群-主群"]
    # Composite 的事件队列来自主 inbound（A），不应被通知预览写成 MESSAGE。
    queued = []
    while not combo.event_queue().empty():
        queued.append(combo.event_queue().get_nowait())
    assert not any(
        ev.event_type == EventType.MESSAGE
        and ev.extra.get("source") == "notification"
        for ev in queued
    )


def test_not_a_complete_source_and_empty_poll_is_not_zero_proof(clock: FakeClock):
    adapter = _adapter(clock)
    assert adapter.is_complete_source() is False
    assert adapter.completeness_guaranteed() is False
    assert adapter.poll_hints() == []
    if hasattr(adapter, "has_no_messages"):
        assert adapter.has_no_messages("任意群") is not True


def test_wake_a_on_preview_false_must_not_auto_wire_wake(clock: FakeClock):
    """配置 wake_a_on_preview=false 时，factory 不得仍把唤醒钩子接到 A。"""
    from src.adapters.factory import build_adapter

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
    assert adapter.notification.wake_callback is None, (
        "wake_a_on_preview=false 时不应自动挂上唤醒回调，"
        f"实际已挂 {adapter.notification.wake_callback}"
    )
    assert adapter.wake_target is None, (
        "wake_a_on_preview=false 时不应把 inbound 设为 wake_target，"
        f"实际为 {type(adapter.wake_target)}"
    )

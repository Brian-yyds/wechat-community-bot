"""读取/发送分离与工厂扩展点。"""

from __future__ import annotations

import pytest

from src.adapters.base import Group
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import AdapterNotImplementedError, build_adapter
from src.adapters.mock import MockAdapter
from src.clock import FakeClock


def test_composite_read_and_send_are_independent(clock: FakeClock):
    inbound = MockAdapter(groups=[Group("g1", "一群")], clock=clock)
    outbound = MockAdapter(groups=[Group("g1", "一群")], clock=clock)
    combo = CompositeAdapter(inbound, outbound)
    received = []
    combo.subscribe(received.append)
    combo.start()
    inbound.inject_message("来自读取侧")
    combo.send_text("g1", "发到发送侧")
    assert any(e.message and e.message.content == "来自读取侧" for e in received)
    assert outbound.sent_messages[0].text == "发到发送侧"
    assert inbound.sent_messages == []
    combo.stop()


def test_build_adapter_mock_and_pending_slots():
    adapter = build_adapter({"wechat": {"inbound": "mock", "outbound": "mock"}})
    assert isinstance(adapter, MockAdapter)
    with pytest.raises(AdapterNotImplementedError, match="尚未在第一阶段实现"):
        build_adapter({"wechat": {"inbound": "local_capture", "outbound": "mock"}})
    with pytest.raises(AdapterNotImplementedError, match="尚未在第一阶段实现"):
        build_adapter({"wechat": {"inbound": "mock", "outbound": "uia"}})

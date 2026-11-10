"""把独立的读取适配器与发送适配器组合成一个 Adapter。"""

from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable

from src.adapters.base import (
    Adapter,
    Event,
    EventCallback,
    Group,
    InboundAdapter,
    OutboundAdapter,
)


class CompositeAdapter(Adapter):
    """inbound 负责收消息，outbound 负责发送。可配置组合，例如 A 读 + B 发。"""

    def __init__(self, inbound: InboundAdapter, outbound: OutboundAdapter) -> None:
        self.inbound = inbound
        self.outbound = outbound

    def start(self) -> None:
        self.outbound.start()
        self.inbound.start()

    def stop(self) -> None:
        self.inbound.stop()
        self.outbound.stop()

    def send_text(self, group_id: str, text: str) -> None:
        self.outbound.send_text(group_id, text)

    def notify_typing(self, group_id: str) -> None:
        self.outbound.notify_typing(group_id)

    def list_groups(self) -> list[Group]:
        return self.inbound.list_groups()

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        return self.inbound.subscribe(callback)

    def event_queue(self) -> queue.Queue[Event]:
        return self.inbound.event_queue()

    def async_event_queue(self) -> asyncio.Queue[Event]:
        return self.inbound.async_event_queue()

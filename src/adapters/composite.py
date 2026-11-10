"""把独立的读取适配器与发送适配器组合成一个 Adapter。"""

from __future__ import annotations

import asyncio
import queue
from collections.abc import Callable
from typing import Any

from src.adapters.base import (
    Adapter,
    Event,
    EventCallback,
    Group,
    InboundAdapter,
    OutboundAdapter,
)


def default_wake_callback(target: Any) -> Callable[[Any], None]:
    """把通知预览转成对主读取适配器的立即轮询。

    优先调用 request_immediate_poll(hint)；若 A 尚未实现该方法，则退回 poll_once / poll。
    """

    def _wake(hint: Any) -> None:
        if hasattr(target, "request_immediate_poll"):
            target.request_immediate_poll(hint)
            return
        if hasattr(target, "poll_once"):
            target.poll_once()
            return
        if hasattr(target, "poll"):
            target.poll()

    return _wake


class CompositeAdapter(Adapter):
    """inbound 负责收消息，outbound 负责发送。可配置组合，例如 A 读 + B 发。

    notification 为可选的 C 旁路：不替代 inbound，只用于唤醒 wake_target
    （默认即 inbound，例如 LocalCaptureAdapter）。
    """

    def __init__(
        self,
        inbound: InboundAdapter,
        outbound: OutboundAdapter,
        *,
        wake_callback: Callable[[Any], None] | None = None,
        wake_target: Any | None = None,
        notification: InboundAdapter | None = None,
    ) -> None:
        self.inbound = inbound
        self.outbound = outbound
        self.notification = notification
        self.wake_target = wake_target
        self.wake_callback = wake_callback
        self._wire_wake()

    def set_wake_callback(self, callback: Callable[[Any], None] | None) -> None:
        self.wake_callback = callback
        self._wire_wake()

    def _wire_wake(self) -> None:
        callback = self.wake_callback
        if callback is None and self.wake_target is not None:
            callback = default_wake_callback(self.wake_target)
        holders = [self.inbound]
        if self.notification is not None:
            holders.append(self.notification)
        for holder in holders:
            if callback is not None and hasattr(holder, "set_wake_callback"):
                holder.set_wake_callback(callback)
            elif callback is not None and hasattr(holder, "wake_callback"):
                holder.wake_callback = callback

    def start(self) -> None:
        self.outbound.start()
        self.inbound.start()
        if self.notification is not None:
            self.notification.start()

    def stop(self) -> None:
        if self.notification is not None:
            self.notification.stop()
        self.inbound.stop()
        self.outbound.stop()

    def send_text(self, group_id: str, text: str, *args, **kwargs) -> None:
        """把 send_id / through_official 等正式出口参数转给 outbound，避免组合层丢弃闸门信息。"""
        self.outbound.send_text(group_id, text, *args, **kwargs)

    @property
    def last_result(self):
        outbound = self.outbound
        result = getattr(outbound, "last_result", None)
        if result is None:
            inner = getattr(outbound, "adapter", None)
            if inner is not None:
                result = getattr(inner, "last_result", None)
        return result

    def attach_official_sender(self, sender: object) -> None:
        """把正式发送闸门挂到真正的 outbound（及可能的内层适配器）。"""
        for target in (self.outbound, getattr(self.outbound, "adapter", None)):
            if target is not None and hasattr(target, "attach_official_sender"):
                target.attach_official_sender(sender)

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

"""协议适配层抽象接口。

本接口是后续接入真实微信协议的稳定契约：收消息事件、发文本、列出群、
启动/停止、事件回调与队列。实现方不应改动本模块的公开签名。
"""

from __future__ import annotations

import asyncio
import queue
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """适配器事件类型。后续可扩展进群/退群等，不影响现有消费者。"""

    MESSAGE = "message"
    ADAPTER_STARTED = "adapter_started"
    ADAPTER_STOPPED = "adapter_stopped"


@dataclass(frozen=True)
class Group:
    """群信息。"""

    group_id: str
    name: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    """一条群文本消息。

    字段说明：
    - message_id: 协议侧消息唯一 ID（模拟适配器会自动生成）。
    - group_id / sender_id: 群与发送者标识，勿在日志中明文打印。
    - content: 文本内容。
    - timestamp: 消息时间（本地 naive datetime）。
    - mentions: 协议层已解析的 @ 列表；管线还会从正文再提取一次。
    - extra: 协议扩展字段，保持向后兼容。
    """

    message_id: str
    group_id: str
    sender_id: str
    content: str
    timestamp: datetime
    sender_name: str = ""
    mentions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    """适配器事件。当前以 MESSAGE 为主，其他类型便于生命周期观察。"""

    event_type: EventType
    timestamp: datetime
    message: Message | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SentMessage:
    """一次发送记录（适配器发出后可留档，便于测试与审计）。"""

    group_id: str
    text: str
    timestamp: datetime
    extra: dict[str, Any] = field(default_factory=dict)


EventCallback = Callable[[Event], None]


class InboundAdapter(ABC):
    """读取适配器：只负责收消息、列群与事件分发。

    后续扩展点：
    - A 本地库只读抓取
    - C 通知预览唤醒
    """

    @abstractmethod
    def start(self) -> None:
        """开始接收事件。"""

    @abstractmethod
    def stop(self) -> None:
        """停止接收。"""

    @abstractmethod
    def list_groups(self) -> list[Group]:
        """列出当前可访问的群。"""

    @abstractmethod
    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """注册事件回调，返回取消订阅函数。"""

    @abstractmethod
    def event_queue(self) -> queue.Queue[Event]:
        """线程安全事件队列。"""

    def async_event_queue(self) -> asyncio.Queue[Event]:
        raise NotImplementedError("该读取适配器暂未实现异步队列")


class OutboundAdapter(ABC):
    """发送适配器：只负责把文本发到群。

    后续扩展点：
    - B UIA 发送
    - D OCR 降级发送
    """

    @abstractmethod
    def send_text(self, group_id: str, text: str) -> None:
        """向指定群发送纯文本。"""

    def notify_typing(self, group_id: str) -> None:
        """可选：模拟「正在输入」。"""
        return None

    def start(self) -> None:
        """若发送通道需要单独启动，子类覆盖。默认空操作。"""
        return None

    def stop(self) -> None:
        """若发送通道需要单独停止，子类覆盖。默认空操作。"""
        return None


class Adapter(InboundAdapter, OutboundAdapter, ABC):
    """读写合一的适配器。当读取与发送由同一实现完成时使用本类。

    也可把独立的 InboundAdapter 与 OutboundAdapter 组合成 CompositeAdapter。
    公开方法签名保持稳定，便于后续接入 A/B/C/D。

    必备能力：
    - start() / stop()：启动与停止收发。
    - send_text(group_id, text)：向群发送文本。
    - list_groups()：列出当前可访问的群。
    - subscribe(callback)：推送式事件回调，可注册多个。
    - event_queue()：拉取式线程安全队列。
    - async_event_queue()：拉取式异步队列（可选使用）。

    可选能力：
    - notify_typing(group_id)：模拟「正在输入」，默认空实现。
    """

    @abstractmethod
    def start(self) -> None:
        """启动适配器，开始接收事件。可重复调用，应幂等。"""

    @abstractmethod
    def stop(self) -> None:
        """停止适配器并释放资源。可重复调用，应幂等。"""

    @abstractmethod
    def send_text(self, group_id: str, text: str) -> None:
        """向指定群发送纯文本消息。"""

    @abstractmethod
    def list_groups(self) -> list[Group]:
        """列出当前可访问的群。"""

    @abstractmethod
    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """注册事件回调，返回取消订阅函数。"""

    @abstractmethod
    def event_queue(self) -> queue.Queue[Event]:
        """返回线程安全事件队列，供拉取式消费。"""

    def async_event_queue(self) -> asyncio.Queue[Event]:
        """返回异步事件队列。默认在首次调用时创建并桥接到 subscribe。

        子类如有更合适的实现可以覆盖。须在存在事件循环的上下文中使用 put。
        """
        raise NotImplementedError("该适配器暂未实现异步队列")

    def notify_typing(self, group_id: str) -> None:
        """可选钩子：通知对端「正在输入」。默认什么也不做。"""
        return None

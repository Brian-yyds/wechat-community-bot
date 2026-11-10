"""协议适配层：稳定接口 + 本地模拟适配器。后续接入真实微信协议时只需实现 Adapter。

读取（InboundAdapter）与发送（OutboundAdapter）可以分离，并用 CompositeAdapter 组合。
"""

from src.adapters.base import (
    Adapter,
    Event,
    EventCallback,
    EventType,
    Group,
    InboundAdapter,
    Message,
    OutboundAdapter,
    SentMessage,
)
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import AdapterNotImplementedError, build_adapter
from src.adapters.mock import MockAdapter

__all__ = [
    "Adapter",
    "AdapterNotImplementedError",
    "CompositeAdapter",
    "Event",
    "EventCallback",
    "EventType",
    "Group",
    "InboundAdapter",
    "Message",
    "MockAdapter",
    "OutboundAdapter",
    "SentMessage",
    "build_adapter",
]

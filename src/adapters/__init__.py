"""协议适配层：稳定接口 + 本地模拟适配器 + 本地库只读抓取（A）+ UIA 发送（B）+ 通知预览（C）+ OCR 降级发送（D）。

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
from src.adapters.local_capture import LocalCaptureAdapter
from src.adapters.local_db import (
    DbKeyProvider,
    EnvFileKeyProvider,
    LocalDbReader,
    ReconcileReport,
    catch_up,
    reconcile,
)
from src.adapters.mock import MockAdapter
from src.adapters.notification import NotificationAdapter, NotificationAssist
from src.adapters.ocr import OcrFallback, OcrOutboundAdapter
from src.adapters.pause import PauseGate, SendPausedError
from src.adapters.uia import UiaOutboundAdapter, UiaSender
from src.adapters.ui_driver import FakeUiDriver, UiDriver

__all__ = [
    "Adapter",
    "AdapterNotImplementedError",
    "CompositeAdapter",
    "DbKeyProvider",
    "EnvFileKeyProvider",
    "Event",
    "EventCallback",
    "EventType",
    "Group",
    "InboundAdapter",
    "LocalCaptureAdapter",
    "LocalDbReader",
    "Message",
    "MockAdapter",
    "NotificationAdapter",
    "NotificationAssist",
    "OcrFallback",
    "OcrOutboundAdapter",
    "PauseGate",
    "SendPausedError",
    "OutboundAdapter",
    "ReconcileReport",
    "SentMessage",
    "UiDriver",
    "FakeUiDriver",
    "UiaOutboundAdapter",
    "UiaSender",
    "build_adapter",
    "catch_up",
    "reconcile",
]

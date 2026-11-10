"""C 方案：通知 / 会话列表预览旁路（辅助唤醒，不是全量消息源）。

本适配器只能在「系统通知或会话列表出现预览」时产出一条不完整事件，
用于提醒或催促 A（本地库只读）立刻轮询。不能、也不声明自己能抓全量群消息。

事件标记 extra["partial"] = True，正文可能被系统截断；免打扰、当前正在查看的会话、
关闭通知的群都可能没有任何提示——没有提示 ≠ 没有新消息。

Windows 真实读取（系统 Toast / 会话列表 UIA）只提供延迟导入的骨架，
尚未在真实微信 4.x 上验证；Linux / box 请使用 FakeNotificationSource。
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.adapters.base import (
    Event,
    EventCallback,
    EventType,
    Group,
    InboundAdapter,
    Message,
)
from src.clock import Clock, SystemClock


# ---------------------------------------------------------------------------
# 预览数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotificationHint:
    """一条可能有新消息的提示。complete 必须为 False。"""

    group: str
    preview: str
    ts: str
    complete: bool = False
    reason: str = "toast"
    sender: str = ""
    group_id: str = ""
    sender_id: str = ""
    partial: bool = True

    def __post_init__(self) -> None:
        # frozen dataclass：用 object.__setattr__ 强制完整=False。
        if self.complete:
            object.__setattr__(self, "complete", False)
        if not self.partial:
            object.__setattr__(self, "partial", True)


@dataclass(frozen=True)
class NotificationPreview:
    """NotificationSource 产出的原始预览。"""

    group_name: str
    sender_name: str = ""
    preview_text: str = ""
    timestamp: datetime | None = None
    group_id: str = ""
    sender_id: str = ""
    suppressed: bool = False
    reason: str = "toast"
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 通知源抽象与实现
# ---------------------------------------------------------------------------


class NotificationSource(ABC):
    """系统通知或会话列表预览的抽象。实现只负责采集预览，不做完整性保证。"""

    @abstractmethod
    def poll(self) -> list[NotificationPreview]:
        """拉取本轮新预览。允许丢失；不得把空列表解释为「没有新消息」。"""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _as_datetime(value: Any, clock: Clock) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        # 允许带时区的 ISO 时间；为与项目其它 naive 时间对齐，去掉 tzinfo。
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return clock.now()
        if parsed.tzinfo is not None:
            return parsed.replace(tzinfo=None)
        return parsed
    return clock.now()


def _hint_to_preview(raw: Any, clock: Clock) -> NotificationPreview:
    if isinstance(raw, NotificationPreview):
        return raw
    if isinstance(raw, NotificationHint):
        return NotificationPreview(
            group_name=raw.group,
            sender_name=raw.sender,
            preview_text=raw.preview,
            timestamp=_as_datetime(raw.ts, clock),
            group_id=raw.group_id,
            sender_id=raw.sender_id,
            reason=raw.reason,
        )
    if isinstance(raw, dict):
        ts = raw.get("timestamp", raw.get("ts"))
        return NotificationPreview(
            group_name=str(raw.get("group") or raw.get("group_name") or ""),
            sender_name=str(raw.get("sender") or raw.get("sender_name") or ""),
            preview_text=str(raw.get("preview") or raw.get("text") or raw.get("preview_text") or ""),
            timestamp=_as_datetime(ts, clock),
            group_id=str(raw.get("group_id") or ""),
            sender_id=str(raw.get("sender_id") or ""),
            suppressed=bool(raw.get("suppressed", False)),
            reason=str(raw.get("reason") or "toast"),
            raw=dict(raw),
        )
    raise TypeError(f"无法把 {type(raw)!r} 转为 NotificationPreview")


class FakeNotificationSource(NotificationSource):
    """供 box / 单测使用的假通知源，不触达系统通知或微信。"""

    def __init__(
        self,
        hints: Iterable[Any] | None = None,
        suppressed: Iterable[str] | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._pending: list[NotificationPreview] = []
        self._lock = threading.Lock()
        self.suppressed: set[str] = set(suppressed or [])
        if hints:
            for item in hints:
                self._pending.append(_hint_to_preview(item, self._clock))

    def push(
        self,
        group: str,
        preview: str,
        *,
        sender: str = "",
        ts: datetime | str | None = None,
        group_id: str = "",
        sender_id: str = "",
        reason: str = "toast",
    ) -> NotificationPreview:
        item = NotificationPreview(
            group_name=group,
            sender_name=sender,
            preview_text=preview,
            timestamp=_as_datetime(ts, self._clock),
            group_id=group_id,
            sender_id=sender_id,
            reason=reason,
        )
        with self._lock:
            self._pending.append(item)
        return item

    def poll(self) -> list[NotificationPreview]:
        with self._lock:
            batch = list(self._pending)
            self._pending.clear()
        return [item for item in batch if not self._is_suppressed(item)]

    def _is_suppressed(self, item: NotificationPreview) -> bool:
        if item.suppressed:
            return True
        if item.group_name in self.suppressed:
            return True
        if item.group_id and item.group_id in self.suppressed:
            return True
        return False


class WindowsNotificationSource(NotificationSource):
    """Windows 系统通知 / 会话列表预览的骨架。

    延迟导入平台依赖，以便本模块在 Linux 上可以安全 import。
    本类尚未在真实微信 4.x 上验证，不得当作已可用实现。
    """

    def __init__(
        self,
        *,
        mode: str = "toast",
        app_name_filter: str = "微信",
    ) -> None:
        self.mode = mode
        self.app_name_filter = app_name_filter
        self._ensure_platform()
        self._ensure_deps()

    def _ensure_platform(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsNotificationSource 只能在 Windows 上使用，"
                f"当前平台是 {sys.platform}。"
                "box / Linux 请使用 FakeNotificationSource。"
                "本骨架尚未在真实微信上验证。"
            )

    def _ensure_deps(self) -> None:
        """延迟导入。缺依赖时给出安装提示，不假装已经调通。"""
        if self.mode == "session_list":
            try:
                import uiautomation  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "读取会话列表预览需要 uiautomation。"
                    "请在 Windows 上执行: pip install uiautomation。"
                    "本骨架尚未在真实微信 4.x 上验证，安装依赖不等于已经可用。"
                ) from exc
            return
        # toast / 通知中心
        errors: list[str] = []
        for name in ("winrt", "winsdk", "windows"):
            try:
                __import__(name)
                return
            except ImportError as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError(
            "读取 Windows 系统通知需要 winrt 或 winsdk（Windows SDK Python 绑定）。"
            "请在 Windows 上安装对应包后再试。"
            "本骨架尚未在真实微信 4.x 上验证，安装依赖不等于已经可用。"
            "已尝试: " + "; ".join(errors)
        )

    def poll(self) -> list[NotificationPreview]:
        raise NotImplementedError(
            "Windows 通知 / 会话列表读取仍是未验证骨架，"
            "不会在未验证时假装已经拿到预览。请使用 FakeNotificationSource 做测试，"
            "并在用户的 Windows 电脑上单独验证后再接入。"
        )


# ---------------------------------------------------------------------------
# InboundAdapter：把预览变成 partial 事件，并可唤醒 A
# ---------------------------------------------------------------------------

WakeCallback = Callable[[NotificationHint], None]


class NotificationAdapter(InboundAdapter):
    """通知预览读取适配器（角色 C，inbound=notification）。

    不是完整消息源。可通过 wake_callback 在收到预览时催促 A 立刻轮询。
    构造时接受 hints / suppressed，以便契约测试直接注入。
    """


    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None = None,
        *,
        groups: list[Group] | None = None,
        clock: Clock | None = None,
        wake_callback: WakeCallback | None = None,
    ) -> "NotificationAdapter":
        """根据 wechat.notification 段构造适配器。"""
        root = config or {}
        section = {}
        for candidate in (
            ((root.get("wechat") or {}).get("notification") if isinstance(root.get("wechat"), dict) else None),
            ((root.get("inbound") or {}).get("notification") if isinstance(root.get("inbound"), dict) else None),
        ):
            if isinstance(candidate, dict):
                section.update(candidate)
        source_name = str(section.get("source") or "fake").strip().lower()
        suppressed = section.get("suppressed_groups") or section.get("suppressed") or []
        interval = float(section.get("poll_interval_seconds", 1.0))
        mode = str(section.get("mode") or "toast")
        clock = clock or SystemClock()
        if source_name == "windows":
            source: NotificationSource = WindowsNotificationSource(
                mode=mode,
                app_name_filter=str(section.get("app_name_filter") or "微信"),
            )
        else:
            source = FakeNotificationSource(suppressed=suppressed, clock=clock)
        return cls(
            source=source,
            suppressed=suppressed,
            wake_callback=wake_callback,
            clock=clock,
            groups=groups,
            poll_interval_seconds=interval,
        )

    def __init__(
        self,
        source: NotificationSource | None = None,
        *,
        hints: Iterable[Any] | None = None,
        suppressed: Iterable[str] | None = None,
        wake_callback: WakeCallback | None = None,
        clock: Clock | None = None,
        groups: list[Group] | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._clock = clock or SystemClock()
        if source is None:
            source = FakeNotificationSource(
                hints=hints,
                suppressed=suppressed,
                clock=self._clock,
            )
        elif hints or suppressed:
            # 允许在已有 source 上再叠加契约测试用的 hints（仅 Fake 支持 push）。
            if hints and hasattr(source, "push"):
                for item in hints:
                    preview = _hint_to_preview(item, self._clock)
                    source.push(  # type: ignore[attr-defined]
                        preview.group_name,
                        preview.preview_text,
                        sender=preview.sender_name,
                        ts=preview.timestamp,
                        group_id=preview.group_id,
                        sender_id=preview.sender_id,
                        reason=preview.reason,
                    )
            if suppressed and hasattr(source, "suppressed"):
                source.suppressed.update(set(suppressed))  # type: ignore[attr-defined]
        self.source = source
        self.wake_callback = wake_callback
        self._groups: list[Group] = list(groups or [])
        self._poll_interval = max(0.05, float(poll_interval_seconds))
        self._callbacks: list[EventCallback] = []
        self._queue: queue.Queue[Event] = queue.Queue()
        self._async_queue: asyncio.Queue[Event] | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._id_seq = 0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seen: set[tuple[str, str, str]] = set()

    # --- 完整性声明（契约要求必须显式返回 False） ---

    def is_complete_source(self) -> bool:
        return False

    def completeness_guaranteed(self) -> bool:
        return False

    def set_wake_callback(self, callback: WakeCallback | None) -> None:
        """供 CompositeAdapter 在运行时挂上 / 更换唤醒钩子。"""
        self.wake_callback = callback

    def poll_hints(self) -> list[NotificationHint]:
        """拉取本轮提示（已过滤免打扰/被抑制的群）。可单独调用，不必先 start。"""
        hints = self._collect_hints()
        if self._running:
            for hint in hints:
                self._emit_and_wake(hint)
        return hints

    def poll_once(self) -> list[NotificationHint]:
        """拉取并分发本轮预览事件（测试与前台循环均可使用）。"""
        hints = self._collect_hints()
        for hint in hints:
            self._emit_and_wake(hint)
        return hints

    # --- InboundAdapter ---

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
        self.source.start()
        self._dispatch(
            Event(event_type=EventType.ADAPTER_STARTED, timestamp=self._clock.now())
        )
        self._thread = threading.Thread(
            target=self._loop,
            name="notification-poll",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.source.stop()
        self._dispatch(
            Event(event_type=EventType.ADAPTER_STOPPED, timestamp=self._clock.now())
        )

    def list_groups(self) -> list[Group]:
        with self._lock:
            return list(self._groups)

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        with self._lock:
            self._callbacks.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return _unsubscribe

    def event_queue(self) -> queue.Queue[Event]:
        return self._queue

    def async_event_queue(self) -> asyncio.Queue[Event]:
        with self._lock:
            if self._async_queue is None:
                self._async_queue = asyncio.Queue()
                try:
                    self._async_loop = asyncio.get_running_loop()
                except RuntimeError:
                    try:
                        self._async_loop = asyncio.get_event_loop()
                    except RuntimeError:
                        self._async_loop = None
            return self._async_queue

    # --- 内部 ---

    def _collect_hints(self) -> list[NotificationHint]:
        previews = self.source.poll()
        out: list[NotificationHint] = []
        for item in previews:
            hint = self._to_hint(item)
            key = (hint.group, hint.preview, hint.ts)
            with self._lock:
                if key in self._seen:
                    continue
                self._seen.add(key)
            out.append(hint)
            self._remember_group(hint)
        return out

    def _remember_group(self, hint: NotificationHint) -> None:
        group_id = hint.group_id or hint.group
        if not group_id:
            return
        with self._lock:
            if any(g.group_id == group_id or g.name == hint.group for g in self._groups):
                return
            self._groups.append(Group(group_id=group_id, name=hint.group or group_id))

    def _to_hint(self, item: NotificationPreview) -> NotificationHint:
        ts = item.timestamp or self._clock.now()
        ts_text = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        return NotificationHint(
            group=item.group_name,
            preview=item.preview_text,
            ts=ts_text,
            complete=False,
            reason=item.reason,
            sender=item.sender_name,
            group_id=item.group_id,
            sender_id=item.sender_id,
            partial=True,
        )

    def _emit_and_wake(self, hint: NotificationHint) -> None:
        event = self._hint_to_event(hint)
        self._dispatch(event)
        callback = self.wake_callback
        if callback is not None:
            callback(hint)

    def _hint_to_event(self, hint: NotificationHint) -> Event:
        with self._lock:
            self._id_seq += 1
            seq = self._id_seq
        ts = _as_datetime(hint.ts, self._clock)
        message = Message(
            message_id=f"notify-{seq}",
            group_id=hint.group_id or hint.group,
            sender_id=hint.sender_id or (hint.sender or "unknown"),
            content=hint.preview,
            timestamp=ts,
            sender_name=hint.sender,
            extra={
                "partial": True,
                "source": "notification",
                "preview": True,
                "complete": False,
                "group_name": hint.group,
                "reason": hint.reason,
            },
        )
        return Event(
            event_type=EventType.MESSAGE,
            timestamp=ts,
            message=message,
            extra={"partial": True, "source": "notification"},
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                # 背景轮询不得把异常冒泡到线程外；下一轮继续。
                pass
            self._stop.wait(self._poll_interval)

    def _dispatch(self, event: Event) -> None:
        self._queue.put(event)
        with self._lock:
            callbacks = list(self._callbacks)
            async_q = self._async_queue
            loop = self._async_loop
        if async_q is not None:
            self._put_async(async_q, event, loop)
        for cb in callbacks:
            cb(event)

    @staticmethod
    def _put_async(
        async_q: asyncio.Queue[Event],
        event: Event,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is loop:
            async_q.put_nowait(event)
            return
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(async_q.put_nowait, event)


# 契约发现优先查找的类名，与 NotificationAdapter 是同一实现。
NotificationAssist = NotificationAdapter

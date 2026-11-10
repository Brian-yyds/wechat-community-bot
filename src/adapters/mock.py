"""本地模拟适配器：从脚本、文件或标准输入注入消息，记录发出的消息，供测试与演示。"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from src.adapters.base import (
    Adapter,
    Event,
    EventCallback,
    EventType,
    Group,
    Message,
    SentMessage,
)
from src.clock import Clock, SystemClock


class MockAdapter(Adapter):
    """不连接真实微信的本地适配器。

    注入方式：
    - inject_message(...) 直接注入一条消息；
    - inject_from_file(path) 读取 JSONL 或 JSON 数组；
    - inject_from_stdin() 读取 JSONL（每行一条）或纯文本（每行作为 content）。

    启动前注入的消息会在 start() 时再分发；启动后注入则立即分发。
    """

    def __init__(
        self,
        groups: list[Group] | None = None,
        clock: Clock | None = None,
        default_group_id: str | None = None,
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._groups: list[Group] = list(
            groups
            or [Group(group_id="demo-group", name="交易会员群演示")]
        )
        self._default_group_id = default_group_id or self._groups[0].group_id
        self._callbacks: list[EventCallback] = []
        self._queue: queue.Queue[Event] = queue.Queue()
        self._async_queue: asyncio.Queue[Event] | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._sent: list[SentMessage] = []
        self._typing: list[tuple[str, datetime]] = []
        self._pending: list[Event] = []
        self._running = False
        self._id_seq = 0
        self._lock = threading.RLock()

    # --- 状态查询（测试用） ---

    @property
    def running(self) -> bool:
        return self._running

    @property
    def sent_messages(self) -> list[SentMessage]:
        with self._lock:
            return list(self._sent)

    @property
    def typing_events(self) -> list[tuple[str, datetime]]:
        with self._lock:
            return list(self._typing)

    def clear_sent(self) -> None:
        with self._lock:
            self._sent.clear()

    # --- Adapter 接口 ---

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            pending = list(self._pending)
            self._pending.clear()
        started = Event(event_type=EventType.ADAPTER_STARTED, timestamp=self._clock.now())
        self._dispatch(started)
        for event in pending:
            self._dispatch(event)

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        stopped = Event(event_type=EventType.ADAPTER_STOPPED, timestamp=self._clock.now())
        self._dispatch(stopped)

    def send_text(self, group_id: str, text: str) -> None:
        record = SentMessage(
            group_id=group_id,
            text=text,
            timestamp=self._clock.now(),
        )
        with self._lock:
            self._sent.append(record)

    def list_groups(self) -> list[Group]:
        with self._lock:
            return list(self._groups)

    def add_group(self, group: Group) -> None:
        with self._lock:
            self._groups.append(group)

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

    def notify_typing(self, group_id: str) -> None:
        with self._lock:
            self._typing.append((group_id, self._clock.now()))

    # --- 注入 ---

    def inject_message(
        self,
        content: str,
        *,
        group_id: str | None = None,
        sender_id: str = "user-demo",
        sender_name: str = "演示用户",
        message_id: str | None = None,
        mentions: Iterable[str] = (),
        timestamp: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Message:
        """注入一条文本消息，并按启动状态分发或暂存。"""
        with self._lock:
            self._id_seq += 1
            seq = self._id_seq
        msg = Message(
            message_id=message_id or f"mock-{seq}",
            group_id=group_id or self._default_group_id,
            sender_id=sender_id,
            content=content,
            timestamp=timestamp or self._clock.now(),
            sender_name=sender_name,
            mentions=tuple(mentions),
            extra=extra or {},
        )
        event = Event(
            event_type=EventType.MESSAGE,
            timestamp=msg.timestamp,
            message=msg,
        )
        with self._lock:
            running = self._running
        if running:
            self._dispatch(event)
        else:
            with self._lock:
                self._pending.append(event)
        return msg

    def inject_from_records(self, records: Iterable[dict[str, Any]]) -> list[Message]:
        """从字典列表注入，字段与 inject_message 参数对应。"""
        messages: list[Message] = []
        for raw in records:
            ts = raw.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            messages.append(
                self.inject_message(
                    content=str(raw.get("content", "")),
                    group_id=raw.get("group_id"),
                    sender_id=raw.get("sender_id", "user-demo"),
                    sender_name=raw.get("sender_name", "演示用户"),
                    message_id=raw.get("message_id"),
                    mentions=raw.get("mentions") or (),
                    timestamp=ts,
                    extra=raw.get("extra"),
                )
            )
        return messages

    def inject_from_file(self, path: str | Path) -> list[Message]:
        """从 JSONL（每行一个对象）或 JSON 数组文件注入消息。"""
        text = Path(path).read_text(encoding="utf-8").strip()
        if not text:
            return []
        records: list[dict[str, Any]]
        if text.startswith("["):
            data = json.loads(text)
            if not isinstance(data, list):
                raise ValueError(f"注入文件应为 JSON 数组或 JSONL: {path}")
            records = data
        else:
            records = []
            for line_no, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"注入文件第 {line_no} 行不是合法 JSON: {path}") from exc
        return self.inject_from_records(records)

    def inject_from_stdin(self, stream: TextIO | None = None) -> list[Message]:
        """从标准输入注入：优先按 JSONL 解析，否则每行当作一条纯文本。"""
        src = stream if stream is not None else sys.stdin
        raw_lines = [line.rstrip("\n") for line in src]
        if not raw_lines:
            return []
        # 尝试按 JSONL 解析全部非空行；若失败则退回纯文本模式。
        records: list[dict[str, Any]] = []
        jsonl_ok = True
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                jsonl_ok = False
                break
            if not isinstance(obj, dict):
                jsonl_ok = False
                break
            records.append(obj)
        if jsonl_ok and records:
            return self.inject_from_records(records)
        messages: list[Message] = []
        for line in raw_lines:
            if line.strip():
                messages.append(self.inject_message(line.strip()))
        return messages

    # --- 内部分发 ---

    def _dispatch(self, event: Event) -> None:
        self._queue.put(event)
        async_q = None
        loop = None
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

"""适配器 A：个人微信本地库只读增量抓取（InboundAdapter）。

通过 LocalDbReader 轮询已解密 / 模拟 SQLite，把新消息转为统一 Message 事件。
默认轮询约 1 秒并带小抖动，事件带 detected_at 便于延迟打点。
不写微信库、不注入进程、不 Hook。
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import random
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from src.adapters.base import (
    Event,
    EventCallback,
    EventType,
    Group,
    InboundAdapter,
    Message,
)
from src.adapters.local_db import (
    CursorCorruptError,
    DbKeyProvider,
    EnvFileKeyProvider,
    LocalDbCorruptError,
    LocalDbError,
    LocalDbLockedError,
    LocalDbMissingError,
    LocalDbReader,
    SchemaMapping,
)
from src.clock import Clock, SystemClock
from src.logging_util import redact

logger = logging.getLogger("wechat_bot.local_capture")

# 默认轮询 1 秒，抖动 ±0.1 秒，使增量检出延迟尽量落在 1 秒附近。
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_POLL_JITTER = 0.1
DEFAULT_RECONCILE_INTERVAL = 60.0


def extract_local_capture_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """合并 inbound.local_capture / wechat.local_capture 等配置段。"""
    data = dict(config or {})
    wechat = data.get("wechat") or {}
    inbound = data.get("inbound") or {}
    section: dict[str, Any] = {}
    for candidate in (
        inbound.get("local_capture") if isinstance(inbound, Mapping) else None,
        wechat.get("local_capture") if isinstance(wechat, Mapping) else None,
        wechat.get("inbound_local_capture") if isinstance(wechat, Mapping) else None,
        data.get("local_capture"),
    ):
        if isinstance(candidate, Mapping):
            section.update(candidate)
    return section


def _as_path(value: str | Path | None, root: Path | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() and root is not None:
        path = root / path
    return path


class LocalCaptureAdapter(InboundAdapter):
    """读取适配器 A：本地库只读抓取，可与 mock / UIA 等发送适配器组合。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        cursor_store: dict[str, Any] | str | Path | None = None,
        *,
        schema: SchemaMapping | dict | None = None,
        group_whitelist: Iterable[str] | None = None,
        self_ids: Iterable[str] | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL,
        poll_jitter_seconds: float = DEFAULT_POLL_JITTER,
        lock_retries: int = 10,
        lock_retry_delay: float = 0.03,
        poll_timeout: float = 2.0,
        clock: Clock | None = None,
        key_provider: DbKeyProvider | None = None,
        require_key: bool = False,
        rng: random.Random | None = None,
        reader: LocalDbReader | None = None,
        groups: list[Group] | None = None,
        seen_ids_max: int = 20000,
        seen_seq_window: int = 10000,
        reconcile_interval_seconds: float = DEFAULT_RECONCILE_INTERVAL,
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._interval = max(0.05, float(poll_interval_seconds))
        self._jitter = max(0.0, float(poll_jitter_seconds))
        self._reconcile_interval = max(0.0, float(reconcile_interval_seconds))
        self._last_reconcile_at: datetime | None = None
        self._rng = rng or random.Random()
        self._callbacks: list[EventCallback] = []
        self._queue: queue.Queue[Event] = queue.Queue()
        self._async_queue: asyncio.Queue[Event] | None = None
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fallback_groups = list(groups or [])
        if reader is not None:
            self._reader = reader
        else:
            if db_path is None:
                raise TypeError("LocalCaptureAdapter 需要 db_path 或 reader")
            self._reader = LocalDbReader(
                db_path,
                cursor_store=cursor_store,
                schema=schema,
                group_whitelist=group_whitelist,
                self_ids=self_ids,
                poll_timeout=poll_timeout,
                lock_retries=lock_retries,
                lock_retry_delay=lock_retry_delay,
                clock=self._clock,
                key_provider=key_provider,
                require_key=require_key,
                seen_ids_max=seen_ids_max,
                seen_seq_window=seen_seq_window,
            )
        self.scheme = "local_capture"

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        groups: list[Group] | None = None,
        clock: Clock | None = None,
        key_provider: DbKeyProvider | None = None,
        project_root: Path | None = None,
    ) -> "LocalCaptureAdapter":
        """按配置构造。db_path 可为空：启动时若库不存在会优雅降级。"""
        data = dict(config or {})
        section = extract_local_capture_config(data)
        bot = data.get("bot") or {}
        identity = bot.get("identity") or {}
        root = project_root or Path.cwd()
        data_dir = _as_path(bot.get("data_dir") or "data", root) or (root / "data")
        env_path = os.environ.get("WECHAT_LOCAL_DB_PATH", "").strip()
        db_path = section.get("db_path") or env_path or ""
        db_resolved = _as_path(db_path, root) if db_path else (root / "data" / "missing-local-capture.db")
        cursor = section.get("cursor_path") or section.get("cursor_store")
        if cursor:
            cursor_store: dict[str, Any] | str | Path | None = _as_path(str(cursor), root)
        else:
            cursor_store = data_dir / "local_capture_cursor.json"
        whitelist = list(section.get("group_whitelist") or [])
        if not whitelist:
            for item in data.get("groups") or []:
                if isinstance(item, Mapping) and item.get("group_id"):
                    whitelist.append(str(item["group_id"]))
                elif isinstance(item, Group):
                    whitelist.append(item.group_id)
        if groups and not (section.get("group_whitelist") or data.get("groups")):
            whitelist = [g.group_id for g in groups]
        self_ids = list(section.get("self_ids") or identity.get("self_ids") or [])
        schema = SchemaMapping.from_dict(section.get("schema") or {})
        require_key = bool(section.get("require_key") or section.get("encrypted"))
        key_file = section.get("key_file")  # 仅作路径，不把密钥写进配置
        strict = section.get("key_file_strict")
        provider = key_provider or EnvFileKeyProvider(
            key_file=key_file,
            strict=None if strict is None else bool(strict),
        )
        return cls(
            db_resolved,
            cursor_store=cursor_store,
            schema=schema,
            group_whitelist=whitelist,
            self_ids=self_ids,
            poll_interval_seconds=float(section.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL)),
            poll_jitter_seconds=float(section.get("poll_jitter_seconds", DEFAULT_POLL_JITTER)),
            lock_retries=int(section.get("lock_retries", 10)),
            lock_retry_delay=float(section.get("lock_retry_delay_seconds", 0.03)),
            poll_timeout=float(section.get("poll_timeout", 2.0)),
            clock=clock,
            key_provider=provider,
            require_key=require_key,
            groups=groups,
            seen_ids_max=int(section.get("seen_ids_max", 20000)),
            seen_seq_window=int(section.get("seen_seq_window", 10000)),
            reconcile_interval_seconds=float(
                section.get("reconcile_interval_seconds", DEFAULT_RECONCILE_INTERVAL)
            ),
        )

    # --- 状态 ---

    @property
    def running(self) -> bool:
        return self._running

    @property
    def reader(self) -> LocalDbReader:
        return self._reader

    def poll(self, limit: int = 200) -> list[dict[str, Any]]:
        """直接拉取一批新消息字典（契约 / 对账 / 自检）。"""
        return self._reader.poll(limit=limit)

    def iter_new(self, limit: int = 10000):
        return self._reader.iter_new(limit=limit)

    def get_cursor(self) -> dict[str, Any]:
        return self._reader.get_cursor()

    def set_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._reader.set_cursor(cursor)

    def assert_readonly(self) -> None:
        self._reader.assert_readonly()

    # --- InboundAdapter ---

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
        self._dispatch(Event(event_type=EventType.ADAPTER_STARTED, timestamp=self._clock.now()))
        thread = threading.Thread(target=self._loop, name="local-capture-poll", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            thread = self._thread
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(2.0, self._interval + self._jitter + 1.0))
        self._reader.close()
        self._dispatch(Event(event_type=EventType.ADAPTER_STOPPED, timestamp=self._clock.now()))

    def list_groups(self) -> list[Group]:
        try:
            rows = self._reader.list_groups_raw()
        except LocalDbError as exc:
            logger.warning("列出群失败，返回配置中的后备列表: %s", redact(str(exc)))
            return list(self._fallback_groups)
        groups = [
            Group(
                group_id=str(r.get("group_id") or ""),
                name=str(r.get("name") or r.get("group_id") or ""),
                extra={
                    "muted": r.get("muted"),
                    "collapsed": r.get("collapsed"),
                    "last_opened_at": r.get("last_opened_at"),
                    # 仅作信息，抓取不会依据这些字段过滤。
                    "ignored_for_capture": False,
                },
            )
            for r in rows
            if r.get("group_id")
        ]
        return groups or list(self._fallback_groups)

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

    # --- 轮询 ---

    def request_immediate_poll(self, hint: object = None) -> list[Message]:
        """供通知旁路（适配器 C）唤醒：立即拉一轮，不等待下一次定时间隔。"""
        return self.poll_once()

    def poll_once(self, limit: int = 200) -> list[Message]:
        """拉一轮并分发事件。供测试注入，不必启动后台线程。"""
        try:
            rows = list(self._reader.poll(limit=limit))
            if self._should_reconcile():
                try:
                    rows.extend(self._reader.reconcile_recent())
                except LocalDbError as exc:
                    logger.warning("定期对账补抓失败: %s", redact(str(exc)))
                else:
                    self._last_reconcile_at = self._clock.now()
        except CursorCorruptError as exc:
            logger.error("游标损坏且无法回退，已停止抓取以免重放: %s", redact(str(exc)))
            raise
        except LocalDbLockedError as exc:
            logger.warning("本地库锁定，本轮跳过并等待下次重试: %s", redact(str(exc)))
            raise
        except LocalDbCorruptError:
            logger.error("本地库损坏，无法继续当作空结果")
            raise
        except LocalDbMissingError:
            logger.warning("本地库不存在，优雅降级为本轮无新消息")
            return []
        messages = [self._to_message(row) for row in rows]
        for msg in messages:
            self._dispatch(
                Event(
                    event_type=EventType.MESSAGE,
                    timestamp=msg.timestamp,
                    message=msg,
                    extra={
                        "detected_at": (msg.extra or {}).get("detected_at"),
                        "source": "local_capture",
                    },
                )
            )
        return messages

    def _should_reconcile(self) -> bool:
        if self._reconcile_interval <= 0:
            return False
        if self._last_reconcile_at is None:
            return True
        now = self._clock.now()
        elapsed = (now - self._last_reconcile_at).total_seconds()
        return elapsed >= self._reconcile_interval

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except LocalDbLockedError:
                pass
            except CursorCorruptError as exc:
                logger.error("游标损坏且无法回退，停止轮询以免重放: %s", redact(str(exc)))
                break
            except LocalDbCorruptError as exc:
                logger.error("本地库损坏，暂停本轮: %s", redact(str(exc)))
            except Exception as exc:  # noqa: BLE001
                logger.exception("本地库轮询异常: %s", redact(str(exc)))
            delay = self._next_delay()
            if self._stop.wait(delay):
                break

    def _next_delay(self) -> float:
        jitter = self._rng.uniform(-self._jitter, self._jitter) if self._jitter else 0.0
        return max(0.05, self._interval + jitter)

    def _to_message(self, row: Mapping[str, Any]) -> Message:
        ts = row.get("timestamp")
        if not isinstance(ts, datetime):
            from src.adapters.local_db import parse_timestamp

            ts = parse_timestamp(row.get("ts") or row.get("timestamp"))
        extra = {
            "is_self": bool(row.get("is_self")),
            "is_text": bool(row.get("is_text", True)),
            "msg_type": row.get("msg_type"),
            "detected_at": row.get("detected_at"),
            "create_seq": row.get("create_seq"),
            "source": row.get("source") or "local_capture",
        }
        return Message(
            message_id=str(row.get("id") or row.get("msg_id")),
            group_id=str(row.get("group_id") or ""),
            sender_id=str(row.get("sender_id") or ""),
            content=str(row.get("content") or row.get("text") or ""),
            timestamp=ts,
            sender_name=str(row.get("sender_name") or ""),
            extra=extra,
        )

    def _dispatch(self, event: Event) -> None:
        self._queue.put(event)
        with self._lock:
            callbacks = list(self._callbacks)
            async_q = self._async_queue
            loop = self._async_loop
        if async_q is not None:
            self._put_async(async_q, event, loop)
        for cb in callbacks:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                logger.exception("事件回调失败: %s", redact(str(exc)))

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

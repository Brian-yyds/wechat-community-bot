"""消息去重：消息 ID + 同群内容哈希，带时间窗。"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from src.adapters.base import Message
from src.clock import Clock, SystemClock


class Deduplicator:
    """在时间窗内，相同 message_id 或同群相同正文视为重复。

    不同群的相同正文不视为重复。超过时间窗后允许再次处理。
    """

    def __init__(
        self,
        window_seconds: float = 600.0,
        max_size: int = 20000,
        clock: Clock | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("去重时间窗必须为正数")
        self.window_seconds = float(window_seconds)
        self.max_size = max_size
        self._clock = clock or SystemClock()
        self.state_path = Path(state_path) if state_path else None
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = threading.Lock()
        self._load_state()

    @staticmethod
    def content_key(message: Message) -> str:
        digest = hashlib.sha256(
            f"{message.group_id}\n{message.content}".encode("utf-8")
        ).hexdigest()
        return f"hash:{digest}"

    @staticmethod
    def id_key(message: Message) -> str:
        return f"id:{message.message_id}"

    def is_duplicate(self, message: Message) -> bool:
        """若为重复则返回 True，并将本次出现记录进时间窗。"""
        now = self._clock.now()
        keys = [self.id_key(message), self.content_key(message)]
        with self._lock:
            self._purge(now)
            expired_before = now - timedelta(seconds=self.window_seconds)
            is_dup = False
            for key in keys:
                seen_at = self._seen.get(key)
                if seen_at is not None and seen_at > expired_before:
                    is_dup = True
                    break
            for key in keys:
                if key in self._seen:
                    self._seen.move_to_end(key)
                self._seen[key] = now
            while len(self._seen) > self.max_size:
                self._seen.popitem(last=False)
            self._save_state_unlocked()
            return is_dup

    def _purge(self, now: datetime) -> None:
        expired_before = now - timedelta(seconds=self.window_seconds)
        stale = [key for key, ts in self._seen.items() if ts <= expired_before]
        for key in stale:
            del self._seen[key]

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("seen") if isinstance(data, dict) else None
        if not isinstance(items, dict):
            return
        for key, value in items.items():
            try:
                self._seen[str(key)] = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue

    def _save_state_unlocked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"seen": {k: v.isoformat() for k, v in self._seen.items()}}
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

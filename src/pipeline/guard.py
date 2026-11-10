"""自动回复守卫：不回自己、不对他人机器人回复、突发熔断。"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.adapters.base import Message
from src.clock import Clock, SystemClock


@dataclass
class IdentityConfig:
    """本账号与他人机器人的识别配置。"""

    self_ids: tuple[str, ...] = ()
    self_names: tuple[str, ...] = ()
    bot_ids: tuple[str, ...] = ()
    bot_names: tuple[str, ...] = ()
    bot_name_keywords: tuple[str, ...] = ("机器人", "小助手")


@dataclass
class StormConfig:
    """防回复风暴：窗口内突发次数超限则熔断一段时间。"""

    group_burst_max: int = 5
    group_window_seconds: float = 60.0
    group_cool_seconds: float = 180.0
    global_burst_max: int = 15
    global_window_seconds: float = 60.0
    global_cool_seconds: float = 180.0


@dataclass
class GuardDecision:
    allow: bool
    reason: str = ""


class ReplyGuard:
    """在真正提交自动回复前做身份与风暴检查。"""

    def __init__(
        self,
        identity: IdentityConfig | None = None,
        storm: StormConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.identity = identity or IdentityConfig()
        self.storm = storm or StormConfig()
        self.clock = clock or SystemClock()
        self._group_events: dict[str, deque[datetime]] = defaultdict(deque)
        self._global_events: deque[datetime] = deque()
        self._group_cool_until: dict[str, datetime] = {}
        self._global_cool_until: datetime | None = None
        self._lock = threading.Lock()

    def is_self(self, message: Message) -> bool:
        extra = message.extra or {}
        if extra.get("from_self") is True:
            return True
        if message.sender_id and message.sender_id in self.identity.self_ids:
            return True
        if message.sender_name and message.sender_name in self.identity.self_names:
            return True
        return False

    def is_foreign_bot(self, message: Message) -> bool:
        extra = message.extra or {}
        if extra.get("is_bot") is True and not self.is_self(message):
            return True
        if message.sender_id and message.sender_id in self.identity.bot_ids:
            return True
        name = message.sender_name or ""
        if name and name in self.identity.bot_names:
            return True
        for kw in self.identity.bot_name_keywords:
            if kw and kw in name and not self.is_self(message):
                # 自己的昵称若含「助手」已在 is_self 放行；此处避免回其他机器人
                if name not in self.identity.self_names:
                    return True
        return False

    def inspect(self, message: Message) -> GuardDecision:
        if self.is_self(message):
            return GuardDecision(False, "self")
        if self.is_foreign_bot(message):
            return GuardDecision(False, "foreign_bot")
        now = self.clock.now()
        with self._lock:
            if self._global_cool_until and now < self._global_cool_until:
                return GuardDecision(False, "global_storm")
            until = self._group_cool_until.get(message.group_id)
            if until and now < until:
                return GuardDecision(False, "group_storm")
        return GuardDecision(True, "")

    def record_reply(self, group_id: str, now: datetime | None = None) -> None:
        """在成功决定回复后记账，用于突发熔断。"""
        now = now or self.clock.now()
        cfg = self.storm
        with self._lock:
            self._trim(self._global_events, now, cfg.global_window_seconds)
            self._global_events.append(now)
            if len(self._global_events) >= cfg.global_burst_max:
                self._global_cool_until = now + timedelta(seconds=cfg.global_cool_seconds)
            bucket = self._group_events[group_id]
            self._trim(bucket, now, cfg.group_window_seconds)
            bucket.append(now)
            if len(bucket) >= cfg.group_burst_max:
                self._group_cool_until[group_id] = now + timedelta(seconds=cfg.group_cool_seconds)

    @staticmethod
    def _trim(bucket: deque[datetime], now: datetime, window: float) -> None:
        cutoff = now - timedelta(seconds=window)
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

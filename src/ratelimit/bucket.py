"""令牌桶限速：全局、单群、以及按发送类型的配额。"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.clock import Clock, SystemClock


@dataclass(frozen=True)
class BucketSpec:
    """单个令牌桶的容量与每秒补充速率。"""

    capacity: float
    refill_per_second: float


@dataclass(frozen=True)
class RateLimitConfig:
    """限速配置。令牌桶负责突发平滑，滑动窗口负责每分钟/每小时上限。"""

    global_bucket: BucketSpec
    per_group: BucketSpec
    announcement: BucketSpec
    auto_reply: BucketSpec
    global_replies_per_hour: int = 40
    group_replies_per_hour: int | None = None
    global_per_minute: int | None = None
    group_per_minute: int | None = None


class TokenBucket:
    """标准令牌桶。"""

    def __init__(self, spec: BucketSpec, clock: Clock | None = None) -> None:
        if spec.capacity <= 0:
            raise ValueError("令牌桶容量必须为正")
        if spec.refill_per_second < 0:
            raise ValueError("补充速率不能为负")
        self.spec = spec
        self._clock = clock or SystemClock()
        self.tokens = float(spec.capacity)
        self._last = self._clock.now()
        self._lock = threading.Lock()

    def refill(self, now: datetime | None = None) -> None:
        now = now or self._clock.now()
        with self._lock:
            self._refill_unlocked(now)

    def _refill_unlocked(self, now: datetime) -> None:
        elapsed = (now - self._last).total_seconds()
        if elapsed > 0:
            self.tokens = min(
                self.spec.capacity,
                self.tokens + elapsed * self.spec.refill_per_second,
            )
            self._last = now

    def allow(self, n: float = 1.0, now: datetime | None = None) -> bool:
        now = now or self._clock.now()
        with self._lock:
            self._refill_unlocked(now)
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    def available(self, now: datetime | None = None) -> float:
        now = now or self._clock.now()
        with self._lock:
            self._refill_unlocked(now)
            return self.tokens

    def time_until_allow(self, n: float = 1.0, now: datetime | None = None) -> float:
        now = now or self._clock.now()
        with self._lock:
            self._refill_unlocked(now)
            if self.tokens >= n:
                return 0.0
            if self.spec.refill_per_second <= 0:
                return float("inf")
            return (n - self.tokens) / self.spec.refill_per_second


class RateLimiter:
    """组合全局、单群、类型配额三类令牌桶。只有全部通过才扣令牌。"""

    def __init__(self, config: RateLimitConfig, clock: Clock | None = None) -> None:
        self.config = config
        self._clock = clock or SystemClock()
        self.global_bucket = TokenBucket(config.global_bucket, clock=self._clock)
        self.announcement_bucket = TokenBucket(config.announcement, clock=self._clock)
        self.auto_reply_bucket = TokenBucket(config.auto_reply, clock=self._clock)
        self._groups: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._global_hour: deque[datetime] = deque()
        self._group_hour: dict[str, deque[datetime]] = defaultdict(deque)
        self._global_minute: deque[datetime] = deque()
        self._group_minute: dict[str, deque[datetime]] = defaultdict(deque)

    def _group_bucket(self, group_id: str) -> TokenBucket:
        with self._lock:
            bucket = self._groups.get(group_id)
            if bucket is None:
                bucket = TokenBucket(self.config.per_group, clock=self._clock)
                self._groups[group_id] = bucket
            return bucket

    def _quota(self, kind: str) -> TokenBucket:
        if kind == "announcement":
            return self.announcement_bucket
        if kind == "auto_reply":
            return self.auto_reply_bucket
        raise ValueError(f"未知发送类型: {kind}")

    def _trim(self, bucket: deque[datetime], now: datetime, window: float) -> None:
        cutoff = now - timedelta(seconds=window)
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _window_blocked(self, group_id: str, kind: str, now: datetime) -> bool:
        if kind != "auto_reply":
            return False
        cfg = self.config
        with self._lock:
            if cfg.global_replies_per_hour:
                self._trim(self._global_hour, now, 3600)
                if len(self._global_hour) >= cfg.global_replies_per_hour:
                    return True
            if cfg.group_replies_per_hour:
                q = self._group_hour[group_id]
                self._trim(q, now, 3600)
                if len(q) >= cfg.group_replies_per_hour:
                    return True
            if cfg.global_per_minute:
                self._trim(self._global_minute, now, 60)
                if len(self._global_minute) >= cfg.global_per_minute:
                    return True
            if cfg.group_per_minute:
                q = self._group_minute[group_id]
                self._trim(q, now, 60)
                if len(q) >= cfg.group_per_minute:
                    return True
        return False

    def _record_windows(self, group_id: str, kind: str, now: datetime) -> None:
        if kind != "auto_reply":
            return
        with self._lock:
            self._global_hour.append(now)
            self._group_hour[group_id].append(now)
            self._global_minute.append(now)
            self._group_minute[group_id].append(now)

    def try_acquire(self, group_id: str, kind: str, now: datetime | None = None) -> bool:
        """尝试同时占用全局、单群与类型配额。失败时不扣减任何桶。"""
        now = now or self._clock.now()
        if self._window_blocked(group_id, kind, now):
            return False
        group = self._group_bucket(group_id)
        quota = self._quota(kind)
        # 为避免部分扣减，先查看再扣。TokenBucket 内部有自己的锁，这里按固定顺序加锁。
        buckets = (self.global_bucket, group, quota)
        # 粗粒度：依次 peek，再消费。因可能并发，使用再检查。
        for bucket in buckets:
            bucket.refill(now)
        if any(bucket.available(now) < 1.0 for bucket in buckets):
            return False
        # 再扣减；极少数并发下可能超发一档，对频率限制可接受。
        # 为更严格，按对象 id 排序加锁后二次确认。
        ordered = sorted(buckets, key=lambda b: id(b))
        for bucket in ordered:
            bucket._lock.acquire()
        try:
            for bucket in buckets:
                bucket._refill_unlocked(now)
            if any(bucket.tokens < 1.0 for bucket in buckets):
                return False
            for bucket in buckets:
                bucket.tokens -= 1.0
            self._record_windows(group_id, kind, now)
            return True
        finally:
            for bucket in reversed(ordered):
                bucket._lock.release()

    def time_until_allow(self, group_id: str, kind: str, now: datetime | None = None) -> float:
        now = now or self._clock.now()
        group = self._group_bucket(group_id)
        quota = self._quota(kind)
        return max(
            self.global_bucket.time_until_allow(1.0, now),
            group.time_until_allow(1.0, now),
            quota.time_until_allow(1.0, now),
        )

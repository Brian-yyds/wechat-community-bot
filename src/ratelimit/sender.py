"""拟人发送器：随机延迟、可选「正在输入」、夜间静默、带优先级的发送队列。

发送出口会强制执行运行模式与紧急暂停（见 ``src.run_mode.SendGate``）：
模式不允许或存在 ``data/PAUSE`` 的发送一律丢弃并记日志。
未注入闸门时保持原有行为，以免破坏既有单测。
"""

from __future__ import annotations

import logging
import random
import threading
import time as time_mod
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import TYPE_CHECKING, Callable

from src.adapters.base import Adapter
from src.clock import Clock, SystemClock
from src.ratelimit.bucket import RateLimitConfig, RateLimiter

if TYPE_CHECKING:
    from src.latency import LatencyStore, LatencyTrace
    from src.run_mode import SendGate


class SendKind(str, Enum):
    ANNOUNCEMENT = "announcement"
    AUTO_REPLY = "auto_reply"


# 数字越小，优先级越高。
DEFAULT_PRIORITY = {
    SendKind.ANNOUNCEMENT: 10,
    SendKind.AUTO_REPLY: 50,
}


@dataclass
class SendItem:
    """队列中的一条待发送消息。"""

    group_id: str
    text: str
    kind: SendKind
    priority: int
    created_at: datetime
    seq: int = 0
    # LatencyTrace，避免发送器与打点模块形成运行时循环导入。
    trace: object | None = None
    rate_wait: float = 0.0
    humanize_delay: float = 0.0
    typing_delay: float = 0.0


@dataclass
class NightSilentConfig:
    enabled: bool = True
    start: time = time(0, 30)
    end: time = time(7, 0)
    announcement_policy: str = "hold"  # hold | drop；静默期内不主动发出
    auto_reply_policy: str = "drop"  # 默认丢弃，不排队补发


@dataclass
class HumanizeConfig:
    send_delay_min: float = 2.0
    send_delay_max: float = 6.0
    typing_hook: bool = True
    typing_duration_min: float = 0.3
    typing_duration_max: float = 1.0
    night: NightSilentConfig = field(default_factory=NightSilentConfig)
    max_rate_wait_seconds: float = 30.0


class HumanizedSender:
    """把要发出的文本排入优先级队列，按拟人节奏与限速真正发送。

    自动回复与定时公告使用不同优先级和独立配额。
    提供 pump() 供测试与演示同步处理队列，不必启动后台线程。
    运行模式与紧急暂停在真正调用适配器发送之前强制检查。
    """

    def __init__(
        self,
        adapter: Adapter,
        limiter: RateLimiter,
        config: HumanizeConfig | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        send_gate: SendGate | None = None,
        latency_store: LatencyStore | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.adapter = adapter
        self.limiter = limiter
        self.config = config or HumanizeConfig()
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        # send_gate 为空时不拦截，保持既有单测行为；build_app 会注入配置闸门。
        self.send_gate = send_gate
        self.latency_store = latency_store
        self.logger = logger or logging.getLogger("wechat_bot")
        self._cv = threading.Condition()
        self._heap: list[tuple[int, int, SendItem]] = []
        self._seq = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.sent_count = 0
        self.dropped_count = 0
        self.held_count = 0

    # --- 供管线 / 调度器调用的接口 ---

    def submit_auto_reply(self, group_id: str, text: str, *, trace: LatencyTrace | None = None) -> SendItem:
        return self.submit(group_id, text, SendKind.AUTO_REPLY, trace=trace)

    def submit_announcement(self, group_id: str, text: str, *, trace: LatencyTrace | None = None) -> SendItem:
        return self.submit(group_id, text, SendKind.ANNOUNCEMENT, trace=trace)

    def submit(
        self,
        group_id: str,
        text: str,
        kind: SendKind | str,
        priority: int | None = None,
        trace: LatencyTrace | None = None,
    ) -> SendItem:
        if isinstance(kind, str):
            kind = SendKind(kind)
        now = self.clock.now()
        if trace is not None:
            trace.queued_at = now
            if not trace.group_id:
                trace.group_id = group_id
            trace.kind = kind.value
        item = SendItem(
            group_id=group_id,
            text=text,
            kind=kind,
            priority=DEFAULT_PRIORITY[kind] if priority is None else priority,
            created_at=now,
            trace=trace,
        )
        with self._cv:
            self._seq += 1
            item.seq = self._seq
            self._heap.append((item.priority, item.seq, item))
            self._heap.sort(key=lambda x: (x[0], x[1]))
            self._cv.notify()
        return item

    def pending_size(self) -> int:
        with self._cv:
            return len(self._heap)

    def start(self) -> None:
        with self._cv:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="humanized-sender",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._running = False
            self._cv.notify_all()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _requeue(self, item: SendItem) -> None:
        with self._cv:
            self._heap.append((item.priority, item.seq, item))
            self._heap.sort(key=lambda x: (x[0], x[1]))

    def pump(self, max_items: int | None = None) -> int:
        """同步处理队列（测试/演示用）。

        夜间被暂存的公告会先放到一边，继续处理可丢弃的自动回复，
        最后再把暂存项放回队列。自动回复默认丢弃、不排队补发。
        """
        sent = 0
        processed = 0
        deferred = 0
        parked: list[SendItem] = []
        pending_hint = max(self.pending_size(), 1)
        limit = max_items if max_items is not None else pending_hint * 10 + 20
        while processed < limit:
            item = self._pop(blocking=False)
            if item is None:
                break
            processed += 1
            result = self._dispatch(item, park_held=True, blocking=False)
            if result == "held":
                parked.append(item)
                continue
            if result == "sent":
                sent += 1
                deferred = 0
            elif result == "deferred":
                parked.append(item)
                deferred += 1
                if deferred >= pending_hint + 2:
                    break
        for item in parked:
            self._requeue(item)
        return sent

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._process_next(blocking=True, hold_requeue=True)

    def _process_next(self, *, blocking: bool, hold_requeue: bool) -> str:
        item = self._pop(blocking=blocking)
        if item is None:
            return "empty"
        return self._dispatch(item, park_held=not hold_requeue, blocking=blocking)

    def _dispatch(self, item: SendItem, *, park_held: bool, blocking: bool = False) -> str:
        # 发送出口先检查运行模式；暂停在真正发出前再查一次，避免延迟期间被紧急叫停。
        dropped = self._drop_if_blocked(item, stage="queue")
        if dropped:
            return "dropped"

        now = self.clock.now()
        policy = self._night_policy(item, now)
        if policy == "drop":
            self.dropped_count += 1
            self._finalize_trace(item, outcome="dropped", reason="夜间静默丢弃")
            return "dropped"
        if policy == "hold":
            self.held_count += 1
            if not park_held:
                self._requeue(item)
            return "held"

        wait = self.limiter.time_until_allow(item.group_id, item.kind.value, now)
        if wait > self.config.max_rate_wait_seconds:
            # 等待过久则先放回，避免阻塞其他类型；测试时钟会记录这次 sleep 前的判断。
            with self._cv:
                self._heap.append((item.priority, item.seq, item))
                self._heap.sort(key=lambda x: (x[0], x[1]))
            if blocking:
                self.clock.sleep(min(1.0, wait))
            return "deferred"
        if wait > 0:
            self.clock.sleep(wait)
            item.rate_wait += wait

        delay = self._random_between(self.config.send_delay_min, self.config.send_delay_max)
        item.humanize_delay = delay
        if delay > 0:
            self.clock.sleep(delay)

        if self.config.typing_hook:
            self.adapter.notify_typing(item.group_id)
            typing = self._random_between(
                self.config.typing_duration_min,
                self.config.typing_duration_max,
            )
            item.typing_delay = typing
            if typing > 0:
                self.clock.sleep(typing)

        # 限速：延迟过后再次获取令牌；若仍不足则短等再试。
        now = self.clock.now()
        if not self.limiter.try_acquire(item.group_id, item.kind.value, now):
            extra = self.limiter.time_until_allow(item.group_id, item.kind.value, now)
            if extra <= 0:
                extra = 0.05
            if extra > self.config.max_rate_wait_seconds:
                with self._cv:
                    self._heap.append((item.priority, item.seq, item))
                    self._heap.sort(key=lambda x: (x[0], x[1]))
                return "deferred"
            self.clock.sleep(extra)
            item.rate_wait += extra
            now = self.clock.now()
            if not self.limiter.try_acquire(item.group_id, item.kind.value, now):
                with self._cv:
                    self._heap.append((item.priority, item.seq, item))
                    self._heap.sort(key=lambda x: (x[0], x[1]))
                return "deferred"

        if self._drop_if_blocked(item, stage="send"):
            return "dropped"

        started = self.clock.now()
        perf_started = time_mod.perf_counter()
        if item.trace is not None:
            item.trace.send_started_at = started
            item.trace.rate_wait_s = item.rate_wait
            item.trace.humanize_delay_s = item.humanize_delay
            item.trace.typing_delay_s = item.typing_delay
        self.adapter.send_text(item.group_id, item.text)
        finished = self.clock.now()
        adapter_s = max((finished - started).total_seconds(), time_mod.perf_counter() - perf_started)
        self.sent_count += 1
        self._finalize_trace(
            item,
            outcome="sent",
            sent_at=finished,
            adapter_send_s=adapter_s,
        )
        return "sent"

    def _drop_if_blocked(self, item: SendItem, *, stage: str) -> bool:
        """发送出口强制闸门。未注入闸门时不拦截。"""
        if self.send_gate is None:
            return False
        decision = self.send_gate.decide(item.kind)
        if decision.allowed:
            return False
        self.dropped_count += 1
        self.send_gate.log_drop(decision, group_id=item.group_id)
        self._finalize_trace(item, outcome="dropped", reason=decision.reason)
        return True

    def _finalize_trace(
        self,
        item: SendItem,
        *,
        outcome: str,
        reason: str = "",
        sent_at: datetime | None = None,
        adapter_send_s: float | None = None,
    ) -> None:
        trace = item.trace
        if trace is None:
            return
        trace.outcome = outcome
        if reason:
            trace.drop_reason = reason
        trace.rate_wait_s = item.rate_wait
        trace.humanize_delay_s = item.humanize_delay
        trace.typing_delay_s = item.typing_delay
        if sent_at is not None:
            trace.sent_at = sent_at
        elif outcome != "pending" and trace.sent_at is None:
            trace.sent_at = self.clock.now()
        if adapter_send_s is not None:
            trace.adapter_send_s = adapter_send_s
        if self.latency_store is not None:
            self.latency_store.write(trace)

    def _pop(self, *, blocking: bool) -> SendItem | None:
        with self._cv:
            if not self._heap:
                if not blocking:
                    return None
                self._cv.wait(timeout=0.2)
                if not self._heap:
                    return None
            _, _, item = self._heap.pop(0)
            return item

    def _night_policy(self, item: SendItem, now: datetime) -> str | None:
        night = self.config.night
        if not night.enabled:
            return None
        if not _in_night(now.time(), night.start, night.end):
            return None
        if item.kind is SendKind.ANNOUNCEMENT:
            return night.announcement_policy
        return night.auto_reply_policy

    def _random_between(self, lo: float, hi: float) -> float:
        if hi <= lo:
            return max(0.0, lo)
        return self.rng.uniform(lo, hi)


def _in_night(current: time, start: time, end: time) -> bool:
    """支持跨日夜间时段，如 23:00-07:00；结束时刻视为开区间。"""
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"无法解析时间: {value}")
    return time(int(parts[0]), int(parts[1]))

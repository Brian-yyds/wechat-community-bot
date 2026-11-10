"""对外约定接口（供核查测试与后续接入使用）。

产品实现位于本仓库 src/ 各模块；本文件将其装配为稳定的门面：
classify / Scheduler / RateLimiter / Sender / humanize_delay_range / redact_log。

不在此实现任何被禁止的业务功能。
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time, timezone
from typing import Any, Iterable, Optional

from src.logging_util import redact
from src.pipeline.classify import SAFE_QUESTION_REPLY, Category, Classifier

SHANGHAI = timezone(timedelta(hours=8))
CATEGORIES = ["提问", "行情讨论", "广告刷屏", "公告回复", "闲聊/无关", "需人工"]
FORBIDDEN_CATEGORY = Category.FORBIDDEN.value

# 拟人发送间隔（秒）：须满足 1.5 ≤ 下限 < 上限 ≤ 8，且上限 + 2s ≤ 12s。
HUMANIZE_DEFAULT = (2.0, 6.0)
NIGHT_SILENCE = ("00:30", "07:00")
CATCH_UP_WINDOW_DEFAULT = 600

_classifier = Classifier()


def classify(message: dict) -> dict:
    """对一条群消息做分类并给出回复策略。

    入参至少包含 text，可选 mentions、quote。
    出参：category, should_reply, reply, flags。
    """
    text = message.get("text") or message.get("content") or ""
    mentions = message.get("mentions") or ()
    quote = ""
    raw_quote = message.get("quote")
    if isinstance(raw_quote, dict):
        quote = raw_quote.get("text") or ""
    elif isinstance(raw_quote, str):
        quote = raw_quote
    category, flags = _classifier.inspect(text, mentions=mentions, quote=quote)
    should_reply = category is Category.QUESTION
    reply = SAFE_QUESTION_REPLY if should_reply else ""
    return {
        "category": category.value,
        "should_reply": should_reply,
        "reply": reply,
        "flags": list(flags),
    }


def humanize_delay_range() -> tuple[float, float]:
    """当前拟人发送延迟区间（秒）。"""
    return HUMANIZE_DEFAULT


def validate_latency_config(p95_max: float = 12.0, process_budget: float = 2.0) -> bool:
    lo, hi = humanize_delay_range()
    if not (1.5 <= lo < hi <= 8.0):
        raise ValueError(f"拟人化区间非法: {(lo, hi)}")
    if hi + process_budget > p95_max:
        raise ValueError("拟人化上限与 P95 目标不相容")
    return True


def redact_log(line: str) -> str:
    """对日志行做脱敏：手机号、wxid、微信号、token/key、邮箱。"""
    return redact(line)


# ---------------------------------------------------------------------------
# 调度：一次性定时任务 + 可注入时钟 + 补发窗口
# ---------------------------------------------------------------------------


class _ClockAdapter:
    """把 now() 调用包一层，缺省使用上海时区当前时间。"""

    def __init__(self, clock=None) -> None:
        self.clock = clock

    def now(self) -> datetime:
        if self.clock is None:
            return datetime.now(SHANGHAI)
        now = self.clock.now()
        if now.tzinfo is None:
            return now.replace(tzinfo=SHANGHAI)
        return now


@dataclass
class Job:
    """一条定时公告任务。"""

    job_id: str
    content: str
    run_at: datetime
    group: str
    status: str = "pending"
    sent_at: datetime | None = None


class Scheduler:
    """配置/代码注入的一次性公告调度。

    必须可注入时钟。tick 返回已到期且尚未发送的任务；
    on_recover 在窗口内补发一次，窗口外标记跳过。
    """

    def __init__(
        self,
        clock=None,
        store=None,
        catch_up_window_seconds: int = CATCH_UP_WINDOW_DEFAULT,
    ) -> None:
        if catch_up_window_seconds < 0 or catch_up_window_seconds > 1800:
            raise ValueError("补发窗口须在 0～1800 秒内")
        self._clock = _ClockAdapter(clock)
        self.store = store if store is not None else {}
        self.catch_up_window_seconds = catch_up_window_seconds
        self._jobs: dict[str, Job] = self.store.setdefault("jobs", {})

    def set_clock(self, clock) -> None:
        self._clock = _ClockAdapter(clock)

    def _now(self, now=None) -> datetime:
        current = now if now is not None else self._clock.now()
        if current.tzinfo is None:
            return current.replace(tzinfo=SHANGHAI)
        return current

    def add_announcement(self, job_id, content, run_at, group) -> None:
        if job_id not in self._jobs:
            self._jobs[job_id] = Job(str(job_id), str(content), run_at, str(group))
            return
        job = self._jobs[job_id]
        if job.status == "pending":
            job.content = str(content)
            job.run_at = run_at
            job.group = str(group)

    def tick(self, now=None) -> list[Job]:
        now = self._now(now)
        due: list[Job] = []
        for job in self._jobs.values():
            if job.status != "pending":
                continue
            run_at = job.run_at
            if run_at.tzinfo is None and now.tzinfo is not None:
                run_at = run_at.replace(tzinfo=now.tzinfo)
            if now >= run_at:
                due.append(job)
        return due

    def mark_sent(self, job_id, sent_at) -> None:
        job = self._jobs[job_id]
        if job.status == "sent":
            return
        job.status = "sent"
        job.sent_at = sent_at

    def mark_skipped(self, job_id) -> None:
        job = self._jobs[job_id]
        if job.status == "sent":
            return
        job.status = "skipped"

    def on_recover(self, now=None) -> list[Job]:
        now = self._now(now)
        out: list[Job] = []
        for job in self._jobs.values():
            if job.status != "pending":
                continue
            run_at = job.run_at
            if run_at.tzinfo is None and now.tzinfo is not None:
                run_at = run_at.replace(tzinfo=now.tzinfo)
            if now < run_at:
                continue
            delta = (now - run_at).total_seconds()
            if delta <= self.catch_up_window_seconds:
                out.append(job)
            else:
                job.status = "skipped"
        return out


# ---------------------------------------------------------------------------
# 限流：滑动窗口 + 同内容冷却 + 夜间静默（不主动发公告）
# ---------------------------------------------------------------------------


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt


class RateLimiter:
    """单群 + 全局限频，夜间静默（主动公告），同内容冷却，随机间隔。"""

    def __init__(
        self,
        group_per_minute: int = 3,
        global_per_minute: int = 10,
        global_per_hour: int = 40,
        group_per_hour: int | None = None,
        content_cooldown_seconds: int = 600,
        night_start: str = NIGHT_SILENCE[0],
        night_end: str = NIGHT_SILENCE[1],
        humanize: tuple[float, float] | None = None,
        rng: random.Random | None = None,
        **kwargs: Any,
    ) -> None:
        # 兼容仅关键字传入的测试调用
        if "group_per_minute" in kwargs:
            group_per_minute = kwargs["group_per_minute"]
        if "global_per_minute" in kwargs:
            global_per_minute = kwargs["global_per_minute"]
        if "content_cooldown_seconds" in kwargs:
            content_cooldown_seconds = kwargs["content_cooldown_seconds"]
        self.group_per_minute = group_per_minute
        self.global_per_minute = global_per_minute
        self.global_per_hour = global_per_hour
        self.group_per_hour = group_per_hour
        self.content_cooldown_seconds = content_cooldown_seconds
        self.night_start = _parse_hhmm(night_start)
        self.night_end = _parse_hhmm(night_end)
        self.humanize = humanize or humanize_delay_range()
        self.rng = rng or random.Random()
        self._events: list[tuple[datetime, str, str, str]] = []

    def in_night_silence(self, now: datetime) -> bool:
        now = _ensure_tz(now)
        current = now.timetz().replace(tzinfo=None)
        start, end = self.night_start, self.night_end
        if start == end:
            return False
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def allow(self, *, group, content_hash, now, kind="reply") -> bool:
        now = _ensure_tz(now)
        window_start = now - timedelta(minutes=1)
        recent = [e for e in self._events if e[0] > window_start]
        if len(recent) >= self.global_per_minute:
            return False
        if sum(1 for e in recent if e[1] == group) >= self.group_per_minute:
            return False
        hour_start = now - timedelta(hours=1)
        hourly = [e for e in self._events if e[0] > hour_start]
        if self.global_per_hour and len(hourly) >= self.global_per_hour:
            return False
        if self.group_per_hour and sum(1 for e in hourly if e[1] == group) >= self.group_per_hour:
            return False
        if content_hash:
            cool = now - timedelta(seconds=self.content_cooldown_seconds)
            for ts, g, _k, h in self._events:
                if h == content_hash and g == group and ts >= cool:
                    return False
        if kind == "announcement" and self.in_night_silence(now):
            return False
        return True

    def record(self, *, group, content_hash, now, kind="reply") -> None:
        now = _ensure_tz(now)
        self._events.append((now, group, kind, content_hash))

    def next_delay(self) -> float:
        lo, hi = self.humanize
        if hi <= lo:
            return max(0.0, lo)
        return self.rng.uniform(lo, hi)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class SendResult:
    ok: bool
    group: str
    text: str
    sent_at: datetime
    delay: float
    skipped: bool = False
    reason: str = ""


class OutboundGateway:
    """可记录发送历史的发送层，供测试断言；不触达真实微信。"""

    def __init__(self, limiter: RateLimiter | None = None, clock=None) -> None:
        self.limiter = limiter or RateLimiter()
        self.clock = clock
        self.history: list[SendResult] = []

    def _now(self) -> datetime:
        if self.clock is None:
            return datetime.now(SHANGHAI)
        now = self.clock.now()
        return _ensure_tz(now)

    def send(self, group, text, *, kind="reply") -> SendResult:
        now = self._now()
        digest = content_hash(text)
        delay = self.limiter.next_delay() if kind != "silent" else 0.0
        if not self.limiter.allow(group=group, content_hash=digest, now=now, kind=kind):
            res = SendResult(False, group, text, now, delay, True, "rate_limited_or_cooled")
            self.history.append(res)
            return res
        self.limiter.record(group=group, content_hash=digest, now=now, kind=kind)
        res = SendResult(True, group, text, now, delay, False, "")
        self.history.append(res)
        return res


# ---------------------------------------------------------------------------
# 自动回复服务（核查约定接口）：冷却、不回自己/机器人、防风暴、游标持久化
# ---------------------------------------------------------------------------


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt


class AutoReplyService:
    """以自动回复为主的处理入口。store 可在进程间共享，用于重启后不重复回复。"""

    def __init__(
        self,
        self_ids=None,
        bot_ids=None,
        store=None,
        clock=None,
        group_cooldown_seconds: float = 60,
        user_cooldown_seconds: float = 120,
        hourly_limit: int = 40,
        group_hourly_limit: int = 20,
        storm_max: int = 3,
        storm_window_seconds: float = 600,
        **kwargs: Any,
    ) -> None:
        self.clock = clock
        self.self_ids = {str(x) for x in (self_ids or ())}
        self.bot_ids = {str(x) for x in (bot_ids or ())}
        self.group_cooldown_seconds = float(group_cooldown_seconds)
        self.user_cooldown_seconds = float(user_cooldown_seconds)
        self.hourly_limit = int(hourly_limit)
        self.group_hourly_limit = int(group_hourly_limit) if group_hourly_limit else 0
        self.storm_max = int(storm_max)
        self.storm_window_seconds = float(storm_window_seconds)
        self.store = store if store is not None else {}
        self.store.setdefault("processed_ids", {})
        self.store.setdefault("group_last", {})
        self.store.setdefault("user_last", {})
        self.store.setdefault("sent_texts", [])
        self.store.setdefault("reply_log", [])  # list of [iso, group]

    def _now(self, now=None) -> datetime:
        current = now if now is not None else (self.clock.now() if self.clock is not None else datetime.now(SHANGHAI))
        return _as_aware(current)

    def handle(self, message: dict, now=None) -> dict:
        now = self._now(now)
        mid = str(message.get("id") or message.get("msg_id") or message.get("message_id") or "")
        group = str(message.get("group") or message.get("group_id") or "")
        sender = str(message.get("sender") or message.get("sender_id") or "")
        text = str(message.get("text") or message.get("content") or "")

        classified = classify(message)
        category = classified.get("category") or ""
        flags = list(classified.get("flags") or [])

        def _out(should: bool, reason: str, reply: str = "") -> dict:
            return {
                "should_reply": should,
                "reason": reason,
                "reply": reply if should else "",
                "category": category,
                "flags": flags,
            }

        processed = self.store["processed_ids"]
        if mid and mid in processed:
            return _out(False, "dedup")

        if message.get("is_self") or sender in self.self_ids:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "self")
        if message.get("is_bot") or sender in self.bot_ids:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "foreign_bot")

        if category == FORBIDDEN_CATEGORY:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "forbidden")
        if category == "广告刷屏":
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "ad")

        # 连锁风暴：不要把我们刚发出的回复再回一遍。
        sent_texts = self.store["sent_texts"]
        if text and text in sent_texts:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "storm")

        last_group = self.store["group_last"].get(group)
        if last_group and self.group_cooldown_seconds > 0:
            prev = _as_aware(datetime.fromisoformat(last_group))
            if prev and (now - prev).total_seconds() < self.group_cooldown_seconds:
                if mid:
                    processed[mid] = now.isoformat()
                return _out(False, "group_cooldown")

        user_key = f"{group}|{sender}"
        last_user = self.store["user_last"].get(user_key)
        if last_user and self.user_cooldown_seconds > 0:
            prev = _as_aware(datetime.fromisoformat(last_user))
            if prev and (now - prev).total_seconds() < self.user_cooldown_seconds:
                if mid:
                    processed[mid] = now.isoformat()
                return _out(False, "user_cooldown")

        hour_ago = now - timedelta(hours=1)
        log = self.store["reply_log"]
        recent = [row for row in log if _as_aware(datetime.fromisoformat(row[0])) > hour_ago]
        if self.hourly_limit and len(recent) >= self.hourly_limit:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "hourly_limit")
        if self.group_hourly_limit:
            group_recent = [row for row in recent if row[1] == group]
            if len(group_recent) >= self.group_hourly_limit:
                if mid:
                    processed[mid] = now.isoformat()
                return _out(False, "group_hourly_limit")

        storm_from = now - timedelta(seconds=self.storm_window_seconds)
        storm_hits = [
            row
            for row in log
            if row[1] == group and _as_aware(datetime.fromisoformat(row[0])) > storm_from
        ]
        if self.storm_max and len(storm_hits) >= self.storm_max:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "storm")

        if not classified.get("should_reply"):
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "no_reply")

        reply = classified.get("reply") or ""
        if not reply:
            if mid:
                processed[mid] = now.isoformat()
            return _out(False, "no_reply")

        if mid:
            processed[mid] = now.isoformat()
        self.store["group_last"][group] = now.isoformat()
        self.store["user_last"][user_key] = now.isoformat()
        self.store["reply_log"].append([now.isoformat(), group])
        if reply not in sent_texts:
            sent_texts.append(reply)
        return _out(True, "ok", reply)


# -*- coding: utf-8 -*-
"""自动回复正确性的假实现，仅供脚手架自检。不是产品代码。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from constants import (
    FORBIDDEN_CATEGORY,
    GROUP_COOLDOWN_SECONDS_DEFAULT,
    GROUP_HOURLY_LIMIT_DEFAULT,
    HOURLY_LIMIT_DEFAULT,
    OVER_LIMIT_POLICY_AUTO_REPLY,
    STORM_MAX_REPLIES,
    STORM_WINDOW_SECONDS,
    USER_COOLDOWN_SECONDS_DEFAULT,
)

SHANGHAI = timezone(timedelta(hours=8))


def _ensure_tz(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(SHANGHAI)
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt


@dataclass
class ReplyDecision:
    should_reply: bool
    reason: str
    reply: str
    category: str = ""
    flags: list | None = None

    def as_dict(self) -> dict:
        return {
            "should_reply": self.should_reply,
            "reason": self.reason,
            "reply": self.reply if self.should_reply else "",
            "category": self.category,
            "flags": list(self.flags or []),
        }


class AutoReplyService:
    """完整覆盖去重、冷却、不回自己/机器人、防风暴、持久化游标、限频、禁止类。"""

    def __init__(
        self,
        *,
        self_ids: Iterable[str] | None = None,
        bot_ids: Iterable[str] | None = None,
        store: dict | None = None,
        clock: Any = None,
        classify=None,
        group_cooldown_seconds: int = GROUP_COOLDOWN_SECONDS_DEFAULT,
        user_cooldown_seconds: int = USER_COOLDOWN_SECONDS_DEFAULT,
        hourly_limit: int = HOURLY_LIMIT_DEFAULT,
        group_hourly_limit: int = GROUP_HOURLY_LIMIT_DEFAULT,
        storm_window_seconds: int = STORM_WINDOW_SECONDS,
        storm_max_replies: int = STORM_MAX_REPLIES,
        over_limit_policy: str = OVER_LIMIT_POLICY_AUTO_REPLY,
        rate_limiter: Any = None,
    ) -> None:
        self.self_ids = set(self_ids or ["bot-self", "交易助手"])
        self.bot_ids = set(bot_ids or ["bot-other", "other-bot"])
        self.store = store if store is not None else {}
        self.clock = clock
        self._classify = classify
        self.group_cooldown_seconds = group_cooldown_seconds
        self.user_cooldown_seconds = user_cooldown_seconds
        self.hourly_limit = hourly_limit
        self.group_hourly_limit = group_hourly_limit
        self.storm_window_seconds = storm_window_seconds
        self.storm_max_replies = storm_max_replies
        self.over_limit_policy = over_limit_policy
        self.rate_limiter = rate_limiter
        self.store.setdefault("processed_ids", {})
        self.store.setdefault("hourly_events", [])  # (ts_iso, group)
        self.store.setdefault("group_last", {})
        self.store.setdefault("user_last", {})
        self.store.setdefault("recent_replies", [])  # (ts_iso, group, text)

    def _now(self, now=None) -> datetime:
        if now is not None:
            return _ensure_tz(now)
        if self.clock is not None:
            return _ensure_tz(self.clock.now())
        return datetime.now(SHANGHAI)

    def handle(self, message: dict, now=None) -> dict:
        now = self._now(now or message.get("ts"))
        msg_id = str(message.get("id") or message.get("msg_id") or "")
        group = message.get("group") or message.get("group_id") or ""
        sender = message.get("sender") or message.get("sender_id") or ""
        text = message.get("text") or message.get("content") or ""
        is_self = bool(message.get("is_self")) or sender in self.self_ids
        is_bot = bool(message.get("is_bot")) or sender in self.bot_ids

        processed = self.store["processed_ids"]
        if msg_id and msg_id in processed:
            return ReplyDecision(False, "dedup", "", flags=["dedup"]).as_dict()

        def _ack(decision: ReplyDecision) -> dict:
            if msg_id:
                processed[msg_id] = now.isoformat()
            return decision.as_dict()

        if is_self:
            return _ack(ReplyDecision(False, "self", ""))
        if is_bot:
            return _ack(ReplyDecision(False, "bot", ""))

        category, flags = "", []
        if self._classify is None:
            try:
                from mock.impl import classify as _cls
            except ImportError:
                from impl import classify as _cls
            result = _cls({"text": text, "mentions": message.get("mentions") or [], "quote": message.get("quote")})
        else:
            result = self._classify(message) if message.get("text") is not None else self._classify({"text": text})
        if isinstance(result, dict):
            category = result.get("category") or ""
            flags = list(result.get("flags") or [])
        else:
            category = str(result)

        if category == FORBIDDEN_CATEGORY or "forbidden_report" in flags:
            return _ack(ReplyDecision(False, "forbidden", "", category, flags))
        if category == "广告刷屏" or "spam" in flags:
            return _ack(ReplyDecision(False, "ads", "", category, flags))

        # 防回复风暴：短窗内同群已回复过多，或对方文本就是我们刚发出的回复
        recent = self.store["recent_replies"]
        window_start = now - timedelta(seconds=self.storm_window_seconds)
        recent_same = [
            ev for ev in recent
            if ev[1] == group and _ensure_tz(datetime.fromisoformat(ev[0])) >= window_start
        ]
        if len(recent_same) >= self.storm_max_replies:
            return _ack(ReplyDecision(False, "storm", "", category, flags + ["storm"]))
        last_texts = {ev[2] for ev in recent_same}
        if text and text in last_texts:
            return _ack(ReplyDecision(False, "storm", "", category, flags + ["storm", "echo"]))

        # 冷却：同群 / 同人
        last_g = self.store["group_last"].get(group)
        if last_g and (now - _ensure_tz(datetime.fromisoformat(last_g))).total_seconds() < self.group_cooldown_seconds:
            return _ack(ReplyDecision(False, "cooldown_group", "", category, flags))
        user_key = f"{group}::{sender}"
        last_u = self.store["user_last"].get(user_key)
        if last_u and (now - _ensure_tz(datetime.fromisoformat(last_u))).total_seconds() < self.user_cooldown_seconds:
            return _ack(ReplyDecision(False, "cooldown_user", "", category, flags))

        # 限频：滑动一小时
        hour_ago = now - timedelta(hours=1)
        events = [
            ev for ev in self.store["hourly_events"]
            if _ensure_tz(datetime.fromisoformat(ev[0])) > hour_ago
        ]
        self.store["hourly_events"] = events
        if len(events) >= self.hourly_limit:
            return _ack(ReplyDecision(False, "rate_limited", "", category, flags + ["rate_limited", self.over_limit_policy]))
        if sum(1 for ev in events if ev[1] == group) >= self.group_hourly_limit:
            return _ack(ReplyDecision(False, "rate_limited_group", "", category, flags + ["rate_limited", self.over_limit_policy]))

        if self.rate_limiter is not None:
            allowed = self.rate_limiter.allow(group=group, content_hash=msg_id or text, now=now, kind="reply")
            if not allowed:
                return _ack(ReplyDecision(False, "rate_limited", "", category, flags + ["rate_limited"]))

        # 仅对「提问」默认回复；其他类别不回（与验收策略一致）
        should = category == "提问"
        reply = ""
        reason = "ok" if should else "no_reply"
        if should:
            if isinstance(result, dict) and result.get("reply"):
                reply = result["reply"]
            else:
                reply = "请查阅群置顶的官方说明或联系管理员。我只提供流程信息，不提供买卖建议。"
            self.store["group_last"][group] = now.isoformat()
            self.store["user_last"][user_key] = now.isoformat()
            self.store["hourly_events"].append((now.isoformat(), group))
            self.store["recent_replies"].append((now.isoformat(), group, reply))
            if self.rate_limiter is not None and hasattr(self.rate_limiter, "record"):
                self.rate_limiter.record(group=group, content_hash=msg_id or text, now=now, kind="reply")
        return _ack(ReplyDecision(should, reason, reply, category, flags))

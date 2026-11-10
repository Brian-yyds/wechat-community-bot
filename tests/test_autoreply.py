# -*- coding: utf-8 -*-
"""自动回复正确性（AR-01～AR-08）：去重、冷却、不回自己/机器人、防风暴、重启不重复、广告与禁止类不回。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adapter import AdapterNotReady, get_impl, use_mock
from constants import FORBIDDEN_CATEGORY

SHANGHAI = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)


class _Clock:
    def __init__(self, now=None):
        self._now = now or T0

    def now(self):
        return self._now

    def advance(self, seconds: float):
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, dt):
        self._now = dt


def _msg(*, mid, text, group="g1", sender="u-member", ts=None, is_self=False, is_bot=False, mentions=None):
    return {
        "id": mid,
        "msg_id": mid,
        "group": group,
        "group_id": group,
        "sender": sender,
        "sender_id": sender,
        "text": text,
        "content": text,
        "ts": ts or T0,
        "is_self": is_self,
        "is_bot": is_bot,
        "mentions": mentions or [],
    }


def _question(mid="q1", **kw):
    kw.setdefault("text", "请问开户需要准备什么资料？")
    return _msg(mid=mid, **kw)


def _make_service(clock, store=None, **kwargs):
    """优先用约定接口 AutoReplyService；mock 模式用假实现；否则尝试用 Pipeline 组装真实路径。"""
    store = store if store is not None else {}
    defaults = dict(
        self_ids=["bot-self", "交易助手"],
        bot_ids=["bot-other", "other-bot"],
        store=store,
        clock=clock,
        group_cooldown_seconds=60,
        user_cooldown_seconds=120,
        hourly_limit=40,
        group_hourly_limit=20,
    )
    defaults.update(kwargs)

    if use_mock():
        from mock.autoreply import AutoReplyService

        return AutoReplyService(**defaults), "mock"

    # 开发模块若已暴露 AutoReplyService / AutoReplier
    try:
        impl = get_impl()
    except AdapterNotReady:
        impl = None
    if impl is not None:
        for name in ("AutoReplyService", "AutoReplier", "ReplyController"):
            cls = getattr(impl, name, None)
            if cls is not None:
                try:
                    return cls(**defaults), f"real:{name}"
                except TypeError:
                    try:
                        return cls(clock=clock, store=store), f"real:{name}"
                    except TypeError:
                        continue

    # 回退：用现有 Pipeline 走真实发送路径（不在测试里补安全闸，缺口应暴露为失败）
    try:
        from src.adapters.base import Message
        from src.clock import FakeClock
        from src.pipeline.classify import Classifier
        from src.pipeline.dedup import Deduplicator
        from src.pipeline.pipeline import Pipeline
        from src.pipeline.reply import ReplyEngine, ReplyRule
    except ImportError as exc:
        pytest.skip(f"未找到 AutoReplyService，且无法组装 Pipeline: {exc}")

    class _RecordingSender:
        def __init__(self):
            self.items: list[tuple[str, str]] = []

        def submit_auto_reply(self, group_id: str, text: str) -> None:
            self.items.append((group_id, text))

    class PipelineHarness:
        """只包装开发已有管线，不额外实现「不回自己」等缺失闸门。"""

        def __init__(self):
            self.store = store
            self.clock = clock
            fc = FakeClock(clock.now().replace(tzinfo=None) if clock.now().tzinfo else clock.now())
            self._fc = fc
            self.sender = _RecordingSender()
            # 一条很宽的规则：用来检验「广告/禁止类也必须被拦住」；若管线没有分类闸门，本测试会失败。
            engine = ReplyEngine(clock=fc)
            engine.add_rule(
                ReplyRule(
                    id="qa-open",
                    reply="请查阅群置顶的官方说明或联系管理员。我只提供流程信息，不提供买卖建议。",
                    priority=10,
                    keywords=("开户", "手续费", "请问"),
                    cooldown_seconds=0,
                )
            )
            engine.add_rule(
                ReplyRule(
                    id="broad",
                    reply="通用回复（若出现在广告/禁止类上即为缺陷）",
                    priority=1,
                    pattern=".+",
                    cooldown_seconds=0,
                )
            )
            self.pipeline = Pipeline(
                dedup=Deduplicator(window_seconds=24 * 3600, clock=fc),
                classifier=Classifier(),
                reply_engine=engine,
                sender=self.sender,
                clock=fc,
            )
            self.self_ids = set(defaults["self_ids"])
            self.bot_ids = set(defaults["bot_ids"])

        def handle(self, message: dict, now=None) -> dict:
            if now is not None:
                n = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
                self._fc.set(n)
            else:
                n = self.clock.now()
                self._fc.set(n.replace(tzinfo=None) if getattr(n, "tzinfo", None) else n)
            mid = str(message.get("id") or message.get("msg_id") or "")
            ts = message.get("ts") or self._fc.now()
            if isinstance(ts, datetime) and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            msg = Message(
                message_id=mid,
                group_id=str(message.get("group") or message.get("group_id") or "g1"),
                sender_id=str(message.get("sender") or message.get("sender_id") or "u"),
                content=str(message.get("text") or message.get("content") or ""),
                timestamp=ts if isinstance(ts, datetime) else self._fc.now(),
                sender_name="",
                mentions=tuple(message.get("mentions") or ()),
            )
            before = len(self.sender.items)
            result = self.pipeline.process(msg)
            replied = len(self.sender.items) > before
            reason = "ok" if replied else "no_reply"
            if result.duplicate:
                reason = "dedup"
            category = result.category.value if result.category is not None else ""
            return {
                "should_reply": replied,
                "reason": reason,
                "reply": result.reply_text or "",
                "category": category,
                "flags": [],
            }

    return PipelineHarness(), "real:pipeline"

def _svc(clock=None, store=None, **kw):
    clock = clock or _Clock()
    svc, _src = _make_service(clock, store=store, **kw)
    inner = svc.handle

    def _handle(message, now=None):
        current = now if now is not None else clock.now()
        message = dict(message)
        message["ts"] = current
        return inner(message, now=current)

    svc.handle = _handle
    return svc, clock


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_ar01_same_msg_id_processed_once():
    """AR-01 同一 msg_id 只处理一次。"""
    svc, clock = _svc()
    first = svc.handle(_question(mid="same-id"))
    second = svc.handle(_question(mid="same-id", text="请问开户需要准备什么资料？（重放）"))
    assert first["should_reply"] is True
    assert second["should_reply"] is False
    assert second.get("reason") in {"dedup", "duplicate"} or second["should_reply"] is False


def test_ar02_group_and_user_cooldown():
    """AR-02 同群 / 同人冷却期内不重复回复。"""
    svc, clock = _svc()
    a = svc.handle(_question(mid="c1", sender="u1", group="g1"))
    assert a["should_reply"] is True
    # 同群另一人，仍在群冷却内
    b = svc.handle(_question(mid="c2", sender="u2", group="g1"))
    assert b["should_reply"] is False, f"同群冷却期内不应再回，实际 {b}"
    clock.advance(61)
    # 群冷却过了，但同人仍在用户冷却（120s）内
    c = svc.handle(_question(mid="c3", sender="u1", group="g1"))
    assert c["should_reply"] is False, f"同人冷却期内不应再回，实际 {c}"
    clock.advance(60)
    d = svc.handle(_question(mid="c4", sender="u1", group="g1"))
    assert d["should_reply"] is True, f"冷却结束后应可再回，实际 {d}"


def test_ar03_do_not_reply_to_self():
    """AR-03 不回自己。"""
    svc, _ = _svc()
    out = svc.handle(_question(mid="s1", sender="bot-self", is_self=True))
    assert out["should_reply"] is False, f"不得回复自己发出的消息: {out}"


def test_ar04_do_not_reply_to_bots():
    """AR-04 不回机器人 / 其他机器人。"""
    svc, _ = _svc()
    out = svc.handle(_question(mid="b1", sender="bot-other", is_bot=True))
    assert out["should_reply"] is False, f"不得回复其他机器人: {out}"


def test_ar05_prevent_reply_storm():
    """AR-05 防回复风暴：连锁回复 / 互相触发。"""
    # 关闭冷却，专门观察风暴闸门
    svc, clock = _svc(group_cooldown_seconds=0, user_cooldown_seconds=0)
    first = svc.handle(_question(mid="st1", sender="u1"))
    assert first["should_reply"] is True
    echo = first["reply"]
    clock.advance(2)
    looped = svc.handle(_msg(mid="st2", text=echo, sender="u-echo", is_bot=False, group="g1"))
    assert looped["should_reply"] is False, f"不得对自身回复再回复（连锁风暴）: {looped}"
    svc2, clock2 = _svc(group_cooldown_seconds=0, user_cooldown_seconds=0)
    sent = 0
    for i in range(6):
        clock2.set(T0 + timedelta(seconds=i * 3))  # 仍在 30s 风暴窗内
        r = svc2.handle(_question(mid=f"storm-{i}", sender=f"u{i}"))
        if r["should_reply"]:
            sent += 1
    assert sent <= 3, f"短窗内回复 {sent} 次，超过风暴上限 3"


def test_ar06_restart_does_not_repeat():
    """AR-06 重启后不重复回复（持久化已处理游标）。"""
    store = {}
    clock = _Clock()
    svc1, _ = _svc(clock=clock, store=store)
    first = svc1.handle(_question(mid="persist-1"))
    assert first["should_reply"] is True
    # 模拟进程重启：新实例、同一 store
    svc2, _ = _svc(clock=clock, store=store)
    second = svc2.handle(_question(mid="persist-1"))
    assert second["should_reply"] is False, (
        f"重启后对已处理 msg_id 再次回复了。已处理游标必须持久化。实际 {second}"
    )


def test_ar07_ads_not_replied():
    """AR-07 广告刷屏不回复。"""
    svc, _ = _svc()
    out = svc.handle(_msg(mid="ad1", text="加微信免费领资料，稳赚日入过万 https://example.com/promo", sender="spammer"))
    assert out["should_reply"] is False, f"广告不得回复: {out}"


def test_ar08_forbidden_not_replied():
    """AR-08 禁止类（博彩/结算/收款等）不回复，仅作为负向拦截。"""
    svc, _ = _svc()
    out = svc.handle(_msg(mid="fb1", text="今晚在群里开盘下注，结算走收款码", sender="u-bad"))
    assert out["should_reply"] is False, f"禁止类不得回复: {out}"
    # 分类若可得，应为禁止功能-上报
    if out.get("category"):
        assert out["category"] == FORBIDDEN_CATEGORY or "forbidden" in (out.get("reason") or "")

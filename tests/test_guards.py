"""自动回复守卫、持久化去重、ReplyProvider。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.adapters.base import Message
from src.clock import FakeClock
from src.pipeline.dedup import Deduplicator
from src.pipeline.guard import IdentityConfig, ReplyGuard, StormConfig
from src.pipeline.pipeline import Pipeline
from src.pipeline.provider import ReplyContext, ReplyProposal, ReplyProvider, RuleReplyProvider
from src.pipeline.reply import ReplyEngine, ReplyRule


def _msg(content: str, *, mid="m1", sender="u1", name="会员", group="g1", extra=None, clock=None) -> Message:
    return Message(
        message_id=mid,
        group_id=group,
        sender_id=sender,
        content=content,
        timestamp=(clock.now() if clock else datetime(2026, 11, 11, 10, 0, 0)),
        sender_name=name,
        extra=extra or {},
    )


def test_do_not_reply_to_self(clock: FakeClock):
    guard = ReplyGuard(
        identity=IdentityConfig(self_ids=("wxid_me",), self_names=("交易助手",)),
        clock=clock,
    )
    assert guard.inspect(_msg("你好", sender="wxid_me", clock=clock)).allow is False
    assert guard.inspect(_msg("你好", sender="other", name="交易助手", clock=clock)).allow is False
    assert guard.inspect(_msg("你好", sender="other", name="会员", clock=clock)).allow is True


def test_do_not_reply_to_foreign_bot(clock: FakeClock):
    guard = ReplyGuard(
        identity=IdentityConfig(bot_ids=("bot-2",), bot_names=("别人的机器人",)),
        clock=clock,
    )
    assert guard.inspect(_msg("你好", sender="bot-2", clock=clock)).reason == "foreign_bot"
    assert guard.inspect(_msg("你好", sender="x", name="别人的机器人", clock=clock)).allow is False
    assert guard.inspect(_msg("你好", sender="x", name="某某机器人助手", clock=clock)).allow is False
    assert guard.inspect(_msg("你好", extra={"is_bot": True}, clock=clock)).allow is False


def test_storm_circuit_breaker(clock: FakeClock):
    guard = ReplyGuard(
        storm=StormConfig(group_burst_max=3, group_window_seconds=60, group_cool_seconds=120),
        clock=clock,
    )
    for i in range(3):
        msg = _msg("hi", mid=str(i), clock=clock)
        assert guard.inspect(msg).allow is True
        guard.record_reply("g1", clock.now())
    assert guard.inspect(_msg("hi", mid="after", clock=clock)).reason == "group_storm"
    clock.advance(121)
    assert guard.inspect(_msg("hi", mid="later", clock=clock)).allow is True


def test_dedup_persists_across_restart(tmp_path: Path, clock: FakeClock):
    path = tmp_path / "processed_messages.json"
    first = Deduplicator(window_seconds=600, clock=clock, state_path=path)
    msg = _msg("同一条", mid="persist-1", clock=clock)
    assert first.is_duplicate(msg) is False
    second = Deduplicator(window_seconds=600, clock=clock, state_path=path)
    assert second.is_duplicate(_msg("同一条", mid="persist-1", clock=clock)) is True


def test_rule_provider_and_pipeline_skips_self(clock: FakeClock):
    class Collector:
        def __init__(self) -> None:
            self.items = []

        def submit_auto_reply(self, group_id, text):
            self.items.append((group_id, text))

    engine = ReplyEngine(clock=clock)
    engine.add_rule(ReplyRule(id="hi", reply="您好", keywords=("你好",)))
    pipe = Pipeline(
        reply_engine=engine,
        reply_provider=RuleReplyProvider(engine),
        reply_guard=ReplyGuard(identity=IdentityConfig(self_ids=("me",)), clock=clock),
        sender=Collector(),
        clock=clock,
    )
    mine = pipe.process(_msg("你好", sender="me", mid="s1", clock=clock))
    other = pipe.process(_msg("你好，在吗", sender="you", mid="s2", clock=clock))
    assert mine.reply_text is None
    assert mine.skip_reason == "self"
    assert other.reply_text == "您好"
    assert pipe.sender.items == [("g1", "您好")]

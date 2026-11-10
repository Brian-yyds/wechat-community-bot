"""消息处理管线：去重、分类、提取、规则回复与落盘。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.adapters.base import Message
from src.clock import FakeClock
from src.pipeline.classify import Category, Classifier
from src.pipeline.dedup import Deduplicator
from src.pipeline.extract import Extractor
from src.pipeline.pipeline import Pipeline
from src.pipeline.reply import ReplyEngine, ReplyRule
from src.pipeline.sink import JsonlSink
from src.safety import ForbiddenContentError


def _msg(content: str, *, mid: str = "m1", group: str = "g1", clock: FakeClock | None = None) -> Message:
    ts = clock.now() if clock else datetime(2026, 11, 11, 10, 0, 0)
    return Message(
        message_id=mid,
        group_id=group,
        sender_id="u1",
        content=content,
        timestamp=ts,
        sender_name="测试",
    )


def test_dedup_same_message_id(clock: FakeClock):
    dedup = Deduplicator(window_seconds=60, clock=clock)
    first = _msg("你好", mid="id-1", clock=clock)
    second = _msg("不同内容", mid="id-1", clock=clock)
    assert dedup.is_duplicate(first) is False
    assert dedup.is_duplicate(second) is True


def test_dedup_same_content_same_group_within_window(clock: FakeClock):
    dedup = Deduplicator(window_seconds=60, clock=clock)
    a = _msg("同一句话", mid="a", clock=clock)
    b = _msg("同一句话", mid="b", clock=clock)
    assert dedup.is_duplicate(a) is False
    assert dedup.is_duplicate(b) is True


def test_dedup_same_content_different_groups_not_duplicate(clock: FakeClock):
    dedup = Deduplicator(window_seconds=60, clock=clock)
    a = _msg("同一句话", mid="a", group="g1", clock=clock)
    b = _msg("同一句话", mid="b", group="g2", clock=clock)
    assert dedup.is_duplicate(a) is False
    assert dedup.is_duplicate(b) is False


def test_dedup_expires_after_window(clock: FakeClock):
    dedup = Deduplicator(window_seconds=60, clock=clock)
    a = _msg("同一句话", mid="a", clock=clock)
    assert dedup.is_duplicate(a) is False
    clock.advance(61)
    b = _msg("同一句话", mid="b", clock=clock)
    assert dedup.is_duplicate(b) is False


def test_classify_categories():
    clf = Classifier()
    assert clf.classify(_msg("请问开户需要准备什么资料？")) is Category.QUESTION
    assert clf.classify(_msg("BTC 今天这根大阴，你们怎么看？")) is Category.MARKET
    assert clf.classify(_msg("收到")) is Category.ANNOUNCE_ACK
    assert clf.classify(_msg("已阅")) is Category.ANNOUNCE_ACK
    assert clf.classify(_msg("哈哈")) is Category.CHAT
    assert clf.classify(_msg("今天中午吃什么")) is Category.CHAT
    assert clf.classify(_msg("加微信免费领取资料")) is Category.AD
    assert clf.classify(_msg("点击链接 https://example.com 免费领取")) is Category.AD
    assert clf.classify(_msg("请管理员立刻看一下，我怀疑账户被盗了")) is Category.NEED_HUMAN
    result = clf.inspect("今晚要不要在群里开盘下注")  # 严禁此类功能：仅验证识别后上报
    assert result[0] is Category.FORBIDDEN
    assert "forbidden_report" in result[1]


def test_extract_links_mentions_keywords_summary():
    ext = Extractor(summary_max_chars=20, max_keywords=8)
    msg = Message(
        message_id="e1",
        group_id="g1",
        sender_id="u1",
        content="请 @管理员 查看今晚交流安排，详情 https://example.com/meet 。风险提示也请同步。",
        timestamp=datetime(2026, 11, 11, 10, 0, 0),
        mentions=("主持",),
    )
    out = ext.extract(msg)
    assert "https://example.com/meet" in out.links
    assert "管理员" in out.mentions
    assert "主持" in out.mentions
    assert out.summary
    assert out.keywords


def test_reply_keyword_regex_priority_cooldown_and_whitelist(clock: FakeClock):
    engine = ReplyEngine(clock=clock)
    engine.add_rule(
        ReplyRule(
            id="low",
            reply="低优先级",
            priority=1,
            keywords=("你好",),
            cooldown_seconds=0,
        )
    )
    engine.add_rule(
        ReplyRule(
            id="high",
            reply="高优先级问候",
            priority=10,
            keywords=("你好", "在吗"),
            cooldown_seconds=60,
            groups=("g1",),
        )
    )
    engine.add_rule(
        ReplyRule(
            id="regex",
            reply="正则命中",
            priority=5,
            pattern=r"怎么看",
        )
    )
    hit = engine.match(_msg("你好，在吗", mid="1", group="g1", clock=clock))
    assert hit is not None and hit.rule.id == "high"
    # 冷却期内不再命中 high，应落到低优先级
    hit2 = engine.match(_msg("你好", mid="2", group="g1", clock=clock))
    assert hit2 is not None and hit2.rule.id == "low"
    # 群白名单：g2 不能用 high
    other = engine.match(_msg("你好", mid="3", group="g2", clock=clock))
    assert other is not None and other.rule.id == "low"
    regex_hit = engine.match(_msg("今天怎么看", mid="4", clock=clock))
    assert regex_hit is not None and regex_hit.rule.id == "regex"


def test_forbidden_reply_rule_rejected():
    engine = ReplyEngine()
    with pytest.raises(ForbiddenContentError):
        engine.add_rule(ReplyRule(id="bad", reply="这是收款说明", keywords=("你好",)))


def test_pipeline_end_to_end_jsonl_and_duplicate(clock: FakeClock, tmp_path: Path):
    class _Collector:
        def __init__(self) -> None:
            self.items: list[tuple[str, str]] = []

        def submit_auto_reply(self, group_id: str, text: str) -> None:
            self.items.append((group_id, text))

    sink = JsonlSink(tmp_path / "records.jsonl")
    engine = ReplyEngine(clock=clock)
    engine.add_rule(ReplyRule(id="hi", reply="您好", keywords=("你好",), cooldown_seconds=0))
    pipe = Pipeline(
        dedup=Deduplicator(window_seconds=60, clock=clock),
        reply_engine=engine,
        sink=sink,
        sender=_Collector(),
        clock=clock,
    )
    first = pipe.process(_msg("你好，在吗？", mid="x", clock=clock))
    second = pipe.process(_msg("你好，在吗？", mid="y", clock=clock))
    assert first.duplicate is False
    assert first.category is Category.CHAT  # 「你好，在吗？」无业务问题，属闲聊/无关
    assert first.reply_id == "hi"  # 仍可由关键词规则回复（演示配置）
    assert pipe.sender.items == [("g1", "您好")]
    assert second.duplicate is True
    assert second.reply_id is None
    records = sink.read_all()
    assert len(records) == 2
    assert records[0]["duplicate"] is False
    assert records[1]["duplicate"] is True
    # 确认是合法 JSONL
    raw = (tmp_path / "records.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(line) for line in raw)

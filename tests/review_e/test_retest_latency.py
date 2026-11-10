# -*- coding: utf-8 -*-
"""复测 5：latency.jsonl 字段、只写哈希、坏行跳过、轮转、p50/p95、3s/8s 判定。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.base import Event, EventType, Message
from src.clock import FakeClock
from src.latency import (
    OPEN_SESSION_TARGET_S,
    OTHER_TARGET_S,
    LatencyStore,
    LatencyTrace,
    percentile,
    summarize_traces,
)
from src.pipeline.pipeline import Pipeline
from src.pipeline.reply import ReplyEngine, ReplyRule

from .helpers import make_sender


def _linear_percentile(values, p):
    if p > 1:
        p = p / 100.0
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _trace(**kwargs) -> LatencyTrace:
    t0 = datetime(2026, 11, 11, 10, 0, 0)
    defaults = dict(
        message_id="m1",
        group_id="g1",
        kind="auto_reply",
        session_open=True,
        chat_was_open=True,
        run_mode="reply",
        reply_id="greeting",
        rule_reply=True,
        outcome="sent",
        preview="今天行情怎么看？有没有风险提示。",
        message_at=t0,
        detected_at=t0 + timedelta(milliseconds=200),
        classified_at=t0 + timedelta(milliseconds=210),
        decided_at=t0 + timedelta(milliseconds=220),
        queued_at=t0 + timedelta(milliseconds=220),
        send_started_at=t0 + timedelta(seconds=2.22),
        sent_at=t0 + timedelta(seconds=2.27),
        decision_s=0.01,
        adapter_send_s=0.05,
        rate_wait_s=0.0,
        humanize_delay_s=2.0,
        typing_delay_s=0.0,
    )
    defaults.update(kwargs)
    return LatencyTrace(**defaults)


def _meets_target(record: dict) -> tuple[bool | None, float, str]:
    """3s/8s 判定：规则回复且已打开会话看 3s，其余看 8s；口径为 e2e_excluding_humanize_s。"""
    segments = record.get("segments") or {}
    metric = segments.get("e2e_excluding_humanize_s")
    rule = bool(record.get("rule_reply"))
    opened = record.get("session_open") is True or record.get("chat_was_open") is True
    target = OPEN_SESSION_TARGET_S if (rule and opened) else OTHER_TARGET_S
    if metric is None:
        return None, target, "missing"
    return metric <= target + 1e-9, target, "ok"


def test_retest_record_has_mode_rule_flag_chat_was_open_and_segments(tmp_path: Path, clock: FakeClock):
    store = LatencyStore(tmp_path / "latency.jsonl")
    engine = ReplyEngine(clock=clock)
    engine.add_rule(ReplyRule(id="greeting", keywords=("你好", "在吗"), reply="您好", cooldown_seconds=0))
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply", latency_store=store)
    pipeline = Pipeline(sender=sender, clock=clock, latency_store=store, reply_engine=engine)
    message = Message(
        message_id="mid-rule-1",
        group_id="demo-group",
        sender_id="user-001",
        content="你好，在吗？这是一段正文明文，不得写入 jsonl。",
        timestamp=clock.now() - timedelta(seconds=1),
        sender_name="会员甲",
        extra={"session_open": True},
    )
    event = Event(event_type=EventType.MESSAGE, timestamp=clock.now(), message=message)
    result = pipeline.handle_event(event)
    assert result and result.reply_text
    sender.pump()
    sent = [r for r in store.read_all() if r.get("outcome") == "sent"]
    assert sent, "规则回复发出后应落盘延迟记录"
    rec = sent[0]
    for key in ("run_mode", "rule_reply", "reply_id", "session_open", "chat_was_open", "kind"):
        assert key in rec, f"记录缺少字段 {key}: {sorted(rec)}"
    assert rec["run_mode"] == "reply"
    assert rec["rule_reply"] is True
    assert rec.get("reply_id")
    assert rec["session_open"] is True
    # Mock 发送通常没有 last_result.chat_was_open，字段必须存在；值可以为空。
    assert "chat_was_open" in rec
    segs = rec.get("segments") or {}
    for key in ("detect_delay_s", "decision_s", "adapter_send_s", "e2e_s", "e2e_excluding_humanize_s"):
        assert key in segs, f"segments 缺少 {key}"
    assert rec.get("message_id") == "mid-rule-1"

    # 直接写入也可保留 chat_was_open。
    rec2 = store.write(_trace(message_id="m-open", chat_was_open=True))
    assert rec2["chat_was_open"] is True
    assert rec2["run_mode"] == "reply"
    assert rec2["rule_reply"] is True


def test_retest_preview_is_length_and_sha256_only(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    body = "今天行情怎么看？有没有风险提示，我想了解一下开户流程。"
    rec = store.write(_trace(preview=body, message_id="m-plain"))
    line = (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    assert body not in line
    assert body not in json.dumps(rec, ensure_ascii=False)
    preview = rec["preview"]
    assert isinstance(preview, dict)
    assert preview.get("chars") == len(body)
    assert isinstance(preview.get("sha256"), str) and len(preview["sha256"]) == 64
    assert set(preview) <= {"chars", "sha256"}


def test_retest_bad_lines_are_skipped_in_read_and_summarize(tmp_path: Path):
    path = tmp_path / "latency.jsonl"
    path.write_text(
        "{not json\n"
        + json.dumps(_trace(message_id="ok1").to_record(), ensure_ascii=False)
        + "\n\n   \n{\"incomplete\": \n"
        + json.dumps(_trace(message_id="ok2").to_record(), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    store = LatencyStore(path)
    rows = store.read_all()
    ids = {r.get("message_id") for r in rows}
    assert ids == {"ok1", "ok2"}
    summary = store.summarize()
    assert summary["count"] == 2


def test_retest_jsonl_rotates_by_size(tmp_path: Path):
    path = tmp_path / "latency.jsonl"
    store = LatencyStore(path, size_limit=400, retain_days=14)
    for i in range(40):
        store.write(_trace(message_id=f"rot-{i}", preview="x" * 20))
    archives = list(tmp_path.glob("latency.jsonl.*"))
    assert archives, "超过 size_limit 后应将旧文件改名为 latency.jsonl.<时间戳>"
    assert path.exists()
    # 当前文件应小于归档前的累计大小，且可读。
    assert store.read_all()
    names = [p.name for p in archives]
    assert all(name.startswith("latency.jsonl.") for name in names)


def test_retest_p50_p95_match_linear_interpolation(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    delays = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    t0 = datetime(2026, 11, 11, 10, 0, 0)
    for i, d in enumerate(delays):
        store.write(
            LatencyTrace(
                message_id=f"m{i}",
                session_open=(i < 5),
                outcome="sent",
                message_at=t0,
                detected_at=t0,
                sent_at=t0 + timedelta(seconds=d),
                humanize_delay_s=0.0,
            )
        )
    summary = store.summarize()
    e2e_all = summary["all"]["by_segment"]["e2e_s"]
    assert e2e_all["p50"] == pytest.approx(_linear_percentile(delays, 50))
    assert e2e_all["p95"] == pytest.approx(_linear_percentile(delays, 95))
    assert percentile(delays, 95) == pytest.approx(9.55)
    assert summary["open_session"]["count"] == 5
    assert summary["other"]["count"] == 5
    assert summary["targets"]["open_session_e2e_s"] == OPEN_SESSION_TARGET_S == 3.0
    assert summary["targets"]["other_e2e_s"] == OTHER_TARGET_S == 8.0


def test_retest_3s_8s_judgment_uses_e2e_excluding_humanize():
    """已打开会话的规则回复目标 3s，其余 8s；评估口径是 e2e_excluding_humanize_s。"""
    assert OPEN_SESSION_TARGET_S == 3.0
    assert OTHER_TARGET_S == 8.0
    open_rule = _trace(
        session_open=True,
        chat_was_open=True,
        rule_reply=True,
        humanize_delay_s=2.0,
        message_at=datetime(2026, 11, 11, 10, 0, 0),
        detected_at=datetime(2026, 11, 11, 10, 0, 0, 100000),
        sent_at=datetime(2026, 11, 11, 10, 0, 2, 400000),
    )
    rec = open_rule.to_record()
    ok, target, _ = _meets_target(rec)
    assert target == 3.0
    # e2e = 2.3s，减去拟人 2.0s → 0.3s，应达标。
    assert rec["segments"]["e2e_excluding_humanize_s"] == pytest.approx(0.3)
    assert ok is True

    other = _trace(
        session_open=False,
        chat_was_open=False,
        rule_reply=False,
        humanize_delay_s=5.0,
        message_at=datetime(2026, 11, 11, 10, 0, 0),
        detected_at=datetime(2026, 11, 11, 10, 0, 0),
        sent_at=datetime(2026, 11, 11, 10, 0, 6),
    ).to_record()
    ok, target, _ = _meets_target(other)
    assert target == 8.0
    assert other["segments"]["e2e_excluding_humanize_s"] == pytest.approx(1.0)
    assert ok is True

    # 若误把拟人算进 3s 预算，打开会话规则回复会假不达标；排除拟人后应达标。
    tight = _trace(
        session_open=True,
        rule_reply=True,
        humanize_delay_s=2.4,
        message_at=datetime(2026, 11, 11, 10, 0, 0),
        detected_at=datetime(2026, 11, 11, 10, 0, 0),
        sent_at=datetime(2026, 11, 11, 10, 0, 2, 800000),
    ).to_record()
    assert tight["segments"]["e2e_s"] == pytest.approx(2.8)
    assert tight["segments"]["e2e_excluding_humanize_s"] == pytest.approx(0.4)
    ok, target, _ = _meets_target(tight)
    assert target == 3.0 and ok is True
    assert tight["segments"]["e2e_s"] <= 3.0  # 含短拟人时仍可落在 3s 附近

    summary = summarize_traces([open_rule, _trace(session_open=False, rule_reply=False, message_id="m2")])
    assert "e2e_excluding_humanize_s" in summary["all"]["by_segment"]
    note = summary["targets"]["note"]
    assert "e2e_excluding_humanize_s" in note
    assert "3" in note and "8" in note

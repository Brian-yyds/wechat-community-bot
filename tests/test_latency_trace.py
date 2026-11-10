"""延迟分段打点：时间戳、分段耗时、汇总统计、自检与脱敏。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from random import Random

import pytest

from src.adapters.base import Event, EventType, Message
from src.adapters.mock import MockAdapter
from src.clock import FakeClock
from src.config_util import load_config
from src.latency import (
    DECISION_TARGET_S,
    OPEN_SESSION_TARGET_S,
    OTHER_TARGET_S,
    LatencyStore,
    LatencyTrace,
    format_summary,
    percentile,
    summarize_traces,
)
from src.logging_util import redact
from src.main import build_app
from src.pipeline.pipeline import Pipeline
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig
from scripts import selfcheck


def _msg(
    clock: FakeClock,
    *,
    content: str = "你好，在吗？",
    message_id: str = "m1",
    ts: datetime | None = None,
    session_open: bool | None = None,
) -> Message:
    extra = {}
    if session_open is not None:
        extra["session_open"] = session_open
    return Message(
        message_id=message_id,
        group_id="demo-group",
        sender_id="user-001",
        content=content,
        timestamp=ts or clock.now(),
        sender_name="会员甲",
        extra=extra,
    )


def test_percentile_p50_p95_max():
    values = [1, 2, 3, 4, 5]
    assert percentile(values, 50) == 3
    assert percentile(values, 0.50) == 3
    assert percentile([10], 95) == 10
    assert percentile([], 50) is None


def test_trace_segments_and_humanize_listed_separately():
    t0 = datetime(2026, 11, 11, 10, 0, 0)
    trace = LatencyTrace(
        message_id="m1",
        group_id="g1",
        session_open=True,
        outcome="sent",
        message_at=t0,
        detected_at=t0 + timedelta(seconds=0.2),
        classified_at=t0 + timedelta(seconds=0.21),
        decided_at=t0 + timedelta(seconds=0.22),
        queued_at=t0 + timedelta(seconds=0.22),
        send_started_at=t0 + timedelta(seconds=3.22),
        sent_at=t0 + timedelta(seconds=3.27),
        decision_s=0.012,
        adapter_send_s=0.05,
        rate_wait_s=0.0,
        humanize_delay_s=3.0,
        typing_delay_s=0.0,
    )
    seg = trace.compute_segments()
    assert seg["detect_delay_s"] == pytest.approx(0.2)
    assert seg["decision_s"] == 0.012
    assert seg["humanize_delay_s"] == 3.0
    assert seg["rate_humanize_delay_s"] == pytest.approx(3.0)
    assert seg["adapter_send_s"] == 0.05
    assert seg["e2e_s"] == pytest.approx(3.07)
    assert seg["e2e_excluding_humanize_s"] == pytest.approx(0.07)
    # 不含拟人时达到 3s 目标；含拟人会超过，所以必须单独列出。
    assert seg["e2e_excluding_humanize_s"] < OPEN_SESSION_TARGET_S
    assert seg["e2e_s"] > OPEN_SESSION_TARGET_S


def test_store_writes_jsonl_and_summarize(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    t0 = datetime(2026, 11, 11, 10, 0, 0)
    traces = []
    for i, delay in enumerate((2.0, 3.0, 4.0, 5.0)):
        tr = LatencyTrace(
            message_id=f"m{i}",
            group_id="g",
            session_open=(i % 2 == 0),
            outcome="sent",
            message_at=t0,
            detected_at=t0 + timedelta(milliseconds=100),
            decided_at=t0 + timedelta(milliseconds=120),
            queued_at=t0 + timedelta(milliseconds=120),
            send_started_at=t0 + timedelta(seconds=delay),
            sent_at=t0 + timedelta(seconds=delay + 0.01),
            decision_s=0.005 + i * 0.001,
            adapter_send_s=0.01,
            humanize_delay_s=delay,
        )
        store.write(tr)
        traces.append(tr)
    rows = store.read_all()
    assert len(rows) == 4
    assert "segments" in rows[0]
    assert "detected_at" in rows[0]
    summary = store.summarize()
    assert summary["count"] == 4
    human = summary["all"]["by_segment"]["humanize_delay_s"]
    assert human["p50"] == 3.5
    assert human["max"] == 5.0
    assert summary["open_session"]["count"] == 2
    assert summary["targets"]["decision_s"] == DECISION_TARGET_S
    assert summary["targets"]["open_session_e2e_s"] == OPEN_SESSION_TARGET_S
    assert summary["targets"]["other_e2e_s"] == OTHER_TARGET_S
    assert "拟人延迟" in summary["targets"]["note"]
    text = format_summary(summary)
    assert "拟人延迟" in text
    assert "单独列出" in text


def test_pipeline_and_sender_fill_all_timestamps(clock: FakeClock, tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(100, 100),
            per_group=BucketSpec(100, 100),
            announcement=BucketSpec(100, 100),
            auto_reply=BucketSpec(100, 100),
        ),
        clock=clock,
    )
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=2.0,
            send_delay_max=2.0,
            open_session_delay_min=2.0,
            open_session_delay_max=2.0,
            typing_hook=False,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
        rng=Random(0),
        latency_store=store,
    )
    from src.pipeline.reply import ReplyEngine, ReplyRule

    engine = ReplyEngine(clock=clock)
    engine.add_rule(ReplyRule(id="greeting", keywords=("你好", "在吗"), reply="您好，我是群助手。", cooldown_seconds=0))
    pipeline = Pipeline(sender=sender, clock=clock, latency_store=store, reply_engine=engine)
    message_at = clock.now() - timedelta(seconds=1)
    detected_at = clock.now()
    event = Event(
        event_type=EventType.MESSAGE,
        timestamp=detected_at,
        message=_msg(clock, ts=message_at, session_open=True),
    )
    result = pipeline.handle_event(event)
    assert result is not None
    assert result.reply_text
    assert result.trace is not None
    assert result.trace.detected_at == detected_at
    assert result.trace.classified_at is not None
    assert result.trace.decided_at is not None
    assert result.trace.queued_at is not None
    assert result.trace.decision_s is not None
    assert result.trace.decision_s < DECISION_TARGET_S

    sent = sender.pump()
    assert sent == 1
    rows = store.read_all()
    # 发送完成后应落盘一条完整记录（不应把决策阶段的 pending 再写一遍）。
    complete = [r for r in rows if r.get("outcome") == "sent"]
    assert len(complete) == 1
    rec = complete[0]
    assert rec["detected_at"]
    assert rec["classified_at"]
    assert rec["decided_at"]
    assert rec["queued_at"]
    assert rec["sent_at"]
    seg = rec["segments"]
    assert seg["detect_delay_s"] == 1.0
    assert seg["humanize_delay_s"] == 2.0
    assert seg["rate_humanize_delay_s"] >= 2.0
    assert seg["e2e_excluding_humanize_s"] is not None
    assert seg["e2e_s"] - seg["humanize_delay_s"] == pytest.approx(seg["e2e_excluding_humanize_s"])
    assert rec["session_open"] is True


def test_decision_time_uses_monotonic_clock_not_fake_clock():
    clock = FakeClock(datetime(2026, 11, 11, 10, 0, 0))
    pipeline = Pipeline(clock=clock)
    result = pipeline.process(_msg(clock, content="哈哈"))
    assert result.trace is not None
    # FakeClock 在决策期间不会被拨快，但单调时钟仍应记录到一个很小的正数或零。
    assert result.trace.decision_s is not None
    assert 0 <= result.trace.decision_s < DECISION_TARGET_S


def test_jsonl_redacts_sensitive_preview(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    trace = LatencyTrace(
        message_id="wxid_secretuser",
        group_id="token abcdef123456",
        preview="请回电话 13800138000，邮箱 demo@example.com",
        outcome="no_send",
        decision_s=0.001,
    )
    record = store.write(trace)
    assert "13800138000" not in str(record)
    assert "abcdef123456" not in str(record)
    assert "wxid_secretuser" not in record["message_id"]
    line = (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    assert "13800138000" not in line
    assert "138****8000" in line or "****" in line
    # 与日志脱敏规则一致
    assert redact("token abcdef123456") in line or "***" in line


def test_selfcheck_latency_outputs_segments(tmp_path: Path, repo_root: Path, monkeypatch):
    ok, msg = selfcheck.check_latency(repo_root, tmp_path / "lat")
    assert ok, msg
    assert "决策耗时" in msg
    assert "拟人延迟" in msg
    assert "适配器发送" in msg
    assert "不含拟人" in msg


def test_build_app_end_to_end_writes_latency_jsonl(tmp_path: Path, repo_root: Path, clock: FakeClock):
    config = load_config(repo_root / "config" / "example.toml", project_root=repo_root)
    config = dict(config)
    config["run"] = {"mode": "reply"}
    rate = dict(config.get("rate_limit") or {})
    rate["send_delay_min"] = 2.0
    rate["send_delay_max"] = 2.0
    rate["typing_hook"] = False
    night = dict(rate.get("night_silent") or {})
    night["enabled"] = False
    rate["night_silent"] = night
    config["rate_limit"] = rate
    app = build_app(config, project_root=repo_root, clock=clock, rng=Random(2), data_dir=tmp_path)
    app["adapter"].start()
    app["adapter"].inject_message("你好，在吗？", sender_id="u1")
    app["sender"].pump()
    app["adapter"].stop()
    path = app["latency_path"]
    assert path.exists()
    rows = app["latency_store"].read_all()
    assert rows
    sent = [r for r in rows if r["outcome"] == "sent"]
    assert sent
    assert sent[0]["segments"]["humanize_delay_s"] == 2.0

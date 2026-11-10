# -*- coding: utf-8 -*-
"""分档拟人延迟、限速窗口持久化、运行模式恢复（补充核查用例，不改核查原文件）。"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.bot_api import (
    OutboundGateway,
    RateLimiter,
    Sender,
    humanize_delay,
    humanize_delay_range,
    validate_latency_config,
)
from src.humanize import (
    DEFAULT_OPEN_SESSION,
    DEFAULT_OTHER,
    profiles_from_mapping,
)
from src.persist import atomic_write_json, read_json
from src.run_mode import SendGate


SHANGHAI = timezone(timedelta(hours=8))


def test_open_session_range_is_short_and_documented():
    lo, hi = humanize_delay_range()
    assert (lo, hi) == DEFAULT_OTHER
    slo, shi = humanize_delay_range("open_session_rule")
    assert (slo, shi) == DEFAULT_OPEN_SESSION
    assert slo == 1.5 and shi == 2.4
    assert shi < hi
    assert shi + 0.5 <= 3.0 + 1e-9
    assert validate_latency_config() is True
    assert validate_latency_config(path="open_session_rule") is True


def test_open_session_floor_overrideable_via_config():
    """产品默认下限 1.5s；核查建议 1.0s 只能通过配置覆盖，不改默认。"""
    profiles = profiles_from_mapping(
        {
            "open_session_delay_min": 1.0,
            "open_session_delay_max": 2.2,
            "humanize_min_floor": 1.0,
        }
    )
    assert profiles.open_session_rule == (1.0, 2.2)
    assert profiles.min_floor == 1.0
    assert validate_latency_config(path="open_session_rule", profiles=profiles) is True
    # 未覆盖时默认仍是 1.5–2.4
    assert humanize_delay_range("open_session_rule") == (1.5, 2.4)


def test_humanize_delay_injectable_rng_and_typing():
    a = [humanize_delay("abc", rng=random.Random(42)) for _ in range(8)]
    b = [humanize_delay("abc", rng=random.Random(42)) for _ in range(8)]
    assert a == b
    short = [humanize_delay("你好", rng=random.Random(i)) for i in range(40)]
    long = [
        humanize_delay(
            "请问现在开户需要准备哪些资料，手续费和提币到账大概要多久？",
            rng=random.Random(i),
        )
        for i in range(40)
    ]
    assert sum(long) / len(long) > sum(short) / len(short) + 0.05
    open_samples = [
        humanize_delay("请问开户要哪些资料？", rng=random.Random(i), path="open_session_rule")
        for i in range(80)
    ]
    slo, shi = humanize_delay_range("open_session_rule")
    assert min(open_samples) >= slo - 1e-9
    assert max(open_samples) <= shi + 1e-9


def test_sender_alias_is_outbound_gateway():
    assert Sender is OutboundGateway


def test_rate_limiter_store_survives_rebuild():
    store: dict = {}
    now = datetime(2026, 11, 11, 16, 0, 0, tzinfo=SHANGHAI)
    a = RateLimiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
        store=store,
    )
    for i in range(40):
        ts = now + timedelta(seconds=i)
        assert a.allow(group="g1", content_hash=f"rs-{i}", now=ts, kind="reply")
        a.record(group="g1", content_hash=f"rs-{i}", now=ts, kind="reply")
    assert "rate_events" in store
    b = RateLimiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
        store=store,
    )
    assert b.allow(group="g1", content_hash="rs-new", now=now + timedelta(seconds=41), kind="reply") is False


def test_rate_limiter_state_file_atomic_and_trims(tmp_path: Path):
    path = tmp_path / "rate_limit_window.json"
    now = datetime(2026, 11, 11, 16, 0, 0, tzinfo=SHANGHAI)
    a = RateLimiter(global_per_hour=2, group_per_hour=2, group_per_minute=20, global_per_minute=20, content_cooldown_seconds=0, state_path=path)
    a.record(group="g1", content_hash="a", now=now, kind="reply")
    a.record(group="g1", content_hash="b", now=now + timedelta(seconds=1), kind="reply")
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["rate_events"]) == 2
    b = RateLimiter(global_per_hour=2, group_per_hour=2, group_per_minute=20, global_per_minute=20, content_cooldown_seconds=0, state_path=path)
    assert b.allow(group="g1", content_hash="c", now=now + timedelta(seconds=2), kind="reply") is False
    # 窗口过期后应重新放行，并裁剪旧事件
    later = now + timedelta(hours=1, seconds=2)
    assert b.allow(group="g1", content_hash="d", now=later, kind="reply") is True
    b.record(group="g1", content_hash="d", now=later, kind="reply")
    leftover = json.loads(path.read_text(encoding="utf-8"))["rate_events"]
    assert len(leftover) == 1
    assert leftover[0]["hash"] == "d"


def test_send_gate_store_survives_rebuild():
    store: dict = {}
    gate = SendGate(store=store, mode="observe")
    assert gate.get_mode() == "observe"
    assert gate.allow_auto_reply() is False
    gate.set_mode("reply")
    assert gate.allow_auto_reply() is True
    assert store.get("run_mode") == "reply"
    restarted = SendGate(store=store, mode="observe")
    assert restarted.get_mode() == "reply"
    restarted.set_mode("announce")
    third = SendGate(store=store)
    assert third.get_mode() == "announce"
    assert third.allow_announcement() is True
    assert third.allow_auto_reply() is False


def test_send_gate_state_wins_over_config_unless_forced(tmp_path: Path):
    path = tmp_path / "run_mode.json"
    gate = SendGate(mode="reply", state_path=path)
    gate.set_mode("reply")
    # 配置仍写 observe、但未 force 时，有效状态优先
    restored = SendGate(mode="observe", state_path=path, config_explicit=False)
    assert restored.get_mode() == "reply"
    # --mode / mode_force 才覆盖状态
    forced = SendGate(mode="observe", state_path=path, config_explicit=True)
    assert forced.get_mode() == "observe"
    # 损坏的状态文件 → 回到 observe
    path.write_text("{not-json", encoding="utf-8")
    broken = SendGate(mode="announce", state_path=path, config_explicit=False)
    assert broken.get_mode() == "observe"
    missing = SendGate(state_path=tmp_path / "no-such.json")
    assert missing.get_mode() == "observe"


def test_atomic_write_json_roundtrip(tmp_path: Path):
    path = tmp_path / "nested" / "state.json"
    atomic_write_json(path, {"ok": 1})
    assert read_json(path) == {"ok": 1}
    assert read_json(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}
    path.write_text("???")
    assert read_json(path, default={"bad": True}) == {"bad": True}


def test_latency_preview_is_fingerprint_and_skips_bad_lines(tmp_path: Path):
    from src.latency import LatencyStore, LatencyTrace

    store = LatencyStore(tmp_path / "latency.jsonl")
    body = "今天行情怎么看？有没有风险提示，我想了解一下开户流程。"
    rec = store.write(LatencyTrace(message_id="m1", preview=body, run_mode="reply", rule_reply=True, reply_id="greeting"))
    assert body not in (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    assert rec["preview"]["chars"] == len(body)
    assert rec["run_mode"] == "reply"
    assert rec["rule_reply"] is True
    path = tmp_path / "latency.jsonl"
    path.write_text("{bad\n" + path.read_text(encoding="utf-8") + "{\n", encoding="utf-8")
    rows = LatencyStore(path).read_all()
    assert any(r.get("message_id") == "m1" for r in rows)


def test_latency_jsonl_rolls_over_when_over_size_limit(tmp_path: Path):
    from src.latency import LatencyStore, LatencyTrace

    path = tmp_path / "latency.jsonl"
    store = LatencyStore(path, size_limit=80, retain_days=14)
    store.write(LatencyTrace(message_id="a", preview="x" * 40))
    store.write(LatencyTrace(message_id="b", preview="y" * 40))
    archived = list(tmp_path.glob("latency.jsonl.*"))
    assert archived, "超过 size_limit 后应把旧文件改名归档"
    assert path.exists()

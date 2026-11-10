# -*- coding: utf-8 -*-
"""延迟打点：字段完整性、p50/p95、坏行容错、轮转、脱敏、3s/8s 目标与拟人下限取舍。"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.base import Event, EventType, Message
from src.bot_api import humanize_delay_range
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
from src.ratelimit.sender import HumanizeConfig, NightSilentConfig

from .helpers import default_windows_limiter, make_app, make_sender


REQUIRED_RECORD_FIELDS = {
    "message_id",
    "session_open",
    "kind",
    "detected_at",
    "decided_at",
    "sent_at",
    "segments",
}
REQUIRED_SEGMENTS = {
    "detect_delay_s",
    "decision_s",
    "humanize_delay_s",
    "adapter_send_s",
    "e2e_s",
    "e2e_excluding_humanize_s",
}


def _trace(**kwargs) -> LatencyTrace:
    t0 = datetime(2026, 11, 11, 10, 0, 0)
    defaults = dict(
        message_id="m1",
        group_id="g1",
        kind="auto_reply",
        session_open=True,
        outcome="sent",
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
        typing_delay_s=0.3,
    )
    defaults.update(kwargs)
    return LatencyTrace(**defaults)


def _linear_percentile(values, p):
    """与 numpy.percentile(..., method='linear') 相同的手算参照：quantile = (n-1)*p。"""
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


def test_percentile_matches_hand_calculated_linear():
    # 手算对照（与 numpy method='linear' 一致）
    assert percentile([1, 2, 3, 4, 5], 50) == pytest.approx(3)
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)  # rank=4*0.95=3.8 → 4*0.2+5*0.8
    assert percentile([1, 2, 3, 4, 5], 0) == pytest.approx(1)
    assert percentile([1, 2, 3, 4, 5], 100) == pytest.approx(5)
    assert percentile([10], 95) == 10
    assert percentile([2.0, 3.0, 4.0, 5.0], 50) == pytest.approx(3.5)
    assert percentile(list(range(1, 21)), 95) == pytest.approx(19.05)
    samples = [
        [1, 2, 3, 4, 5],
        [10],
        [2.0, 3.0, 4.0, 5.0],
        list(range(1, 21)),
        [0.1, 0.2, 0.4, 1.5, 3.0, 8.0, 9.5],
    ]
    for values in samples:
        for p in (50, 95, 0, 100, 0.5, 0.95):
            got = percentile(values, p)
            expected = _linear_percentile(values, p)
            assert got == pytest.approx(expected), f"values={values} p={p} got={got} expected={expected}"
    assert percentile([], 50) is None


def test_summarize_p50_p95_against_hand_calc(tmp_path: Path):
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
    assert e2e_all["max"] == 10.0
    assert summary["open_session"]["count"] == 5
    assert summary["other"]["count"] == 5


def test_empty_store_is_tolerant(tmp_path: Path):
    store = LatencyStore(tmp_path / "missing" / "latency.jsonl")
    assert store.read_all() == []
    summary = store.summarize()
    assert summary["count"] == 0
    assert summary["all"]["by_segment"]["e2e_s"]["p50"] is None


def test_bad_jsonl_lines_must_be_skipped(tmp_path: Path):
    path = tmp_path / "latency.jsonl"
    good = _trace(message_id="ok1").to_record()
    path.write_text(
        "{not json\n"
        + json.dumps(good, ensure_ascii=False)
        + "\n\n   \n{\"incomplete\": \n"
        + json.dumps(_trace(message_id="ok2").to_record(), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    store = LatencyStore(path)
    try:
        rows = store.read_all()
    except json.JSONDecodeError as exc:
        pytest.fail(f"坏行应被跳过并继续汇总，实际 read_all 抛 JSONDecodeError: {exc}")
    ids = {r.get("message_id") for r in rows}
    assert "ok1" in ids and "ok2" in ids
    assert store.summarize()["count"] >= 2


def test_record_fields_include_mode_and_rule_flag(tmp_path: Path, clock: FakeClock):
    store = LatencyStore(tmp_path / "latency.jsonl")
    engine = ReplyEngine(clock=clock)
    engine.add_rule(ReplyRule(id="greeting", keywords=("你好", "在吗"), reply="您好", cooldown_seconds=0))
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply", latency_store=store)
    pipeline = Pipeline(sender=sender, clock=clock, latency_store=store, reply_engine=engine)
    message = Message(
        message_id="mid-rule-1",
        group_id="demo-group",
        sender_id="user-001",
        content="你好，在吗？",
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
    missing = REQUIRED_RECORD_FIELDS - set(rec)
    assert not missing, f"记录缺少字段 {missing}: {sorted(rec)}"
    seg_missing = REQUIRED_SEGMENTS - set(rec.get("segments") or {})
    assert not seg_missing, f"segments 缺少 {seg_missing}"
    # 验收要求：模式、规则回复标记必须在记录中。
    has_mode = "mode" in rec or "run_mode" in rec or (isinstance(rec.get("extra"), dict) and "mode" in rec["extra"])
    has_rule = any(
        key in rec or (isinstance(rec.get("extra"), dict) and key in rec["extra"])
        for key in ("rule_reply", "is_rule_reply", "rule_id", "reply_id")
    )
    assert has_mode, f"latency.jsonl 缺少运行模式字段，实际键={sorted(rec)}"
    assert has_rule, f"latency.jsonl 缺少规则回复标记，实际键={sorted(rec)}"
    assert rec.get("message_id") == "mid-rule-1"
    assert rec.get("session_open") is True


def test_preview_must_not_keep_message_plaintext(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    body = "今天行情怎么看？有没有风险提示，我想了解一下开户流程。"
    rec = store.write(_trace(preview=body, message_id="m-plain"))
    line = (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    assert body not in line, "latency.jsonl 不得保存消息正文明文（preview 目前写入了正文截断）"
    assert body not in json.dumps(rec, ensure_ascii=False)


def test_redacts_phone_wxid_token_in_jsonl(tmp_path: Path):
    store = LatencyStore(tmp_path / "latency.jsonl")
    store.write(
        _trace(
            message_id="wxid_secretuser",
            group_id="token abcdef123456",
            preview="回拨 13912345678，token=supersecretvalue",
            drop_reason="user wxid_secretuser token=supersecretvalue",
        )
    )
    line = (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    for secret in ("wxid_secretuser", "abcdef123456", "13912345678", "supersecretvalue"):
        assert secret not in line, f"敏感信息残留: {secret!r} in {line}"


def test_no_rotation_or_size_cap_api():
    assert not hasattr(LatencyStore, "rotate"), "当前无轮转接口（将在报告中作为文件增长风险）"
    assert not hasattr(LatencyStore, "max_bytes")
    assert not hasattr(LatencyStore, "max_records")
    # 文档式断言：write 只追加。增长率问题记入报告，此处确认 API 缺失。
    src = Path(__file__).resolve().parents[2] / "src" / "latency.py"
    text = src.read_text(encoding="utf-8")
    assert "rotate" not in text and "max_bytes" not in text


def test_e2e_excluding_humanize_does_not_subtract_typing():
    tr = _trace(humanize_delay_s=2.0, typing_delay_s=0.8)
    seg = tr.compute_segments()
    # 现状：只减 humanize_delay_s，不减 typing_delay_s。
    expected_only_humanize = seg["e2e_s"] - 2.0
    assert seg["e2e_excluding_humanize_s"] == pytest.approx(expected_only_humanize)
    # 若把打字也视为拟人，排除后还应再减 0.8s。
    # 这个差异在 3s 预算里需要被明确取舍，此处记录现状。
    assert tr.typing_delay_s == 0.8


def test_target_constants_are_3s_and_8s_not_hard_2s_or_unban():
    assert OPEN_SESSION_TARGET_S == 3.0
    assert OTHER_TARGET_S == 8.0


def test_open_session_rule_humanize_range_fits_3s_budget():
    """工作区未提交补丁若已提供分档，短区间上限 + 0.5s 预算应不超过 3s。"""
    sig = inspect.signature(humanize_delay_range)
    accepts_path = "path" in sig.parameters
    lo_default, hi_default = humanize_delay_range()
    assert (lo_default, hi_default) == (2.0, 6.0)
    assert accepts_path, (
        "src.bot_api.humanize_delay_range 仍不接受 path='open_session_rule'。"
        "默认 2–6s 与已打开会话 3s 目标冲突，必须提供短区间分档。"
    )
    lo, hi = humanize_delay_range("open_session_rule")
    assert 1.5 <= lo < hi <= 2.5, f"open_session_rule 区间异常: {(lo, hi)}"
    assert hi + 0.5 <= OPEN_SESSION_TARGET_S + 1e-9, (
        f"短区间上限 {hi}s + 检出/决策预算 0.5s 应 ≤ 3s，实际 {hi + 0.5}"
    )


def test_default_other_range_vs_targets():
    """取舍：其余档 2–6s（可再加打字 0.3–1.0s）对准 8s 目标；不得套用到已打开会话 3s。"""
    lo, hi = humanize_delay_range()
    assert lo >= 2.0
    assert hi <= 6.0
    assert hi + 1.0 <= OTHER_TARGET_S
    assert hi + 0.3 > OPEN_SESSION_TARGET_S


def test_mock_detect_delay_is_zero_in_build_app(tmp_path: Path, clock: FakeClock):
    """开发已声明：Mock 检出延迟为 0。此处确认现状，真机须另测。"""
    app = make_app(tmp_path, clock, mode="reply")
    app["adapter"].start()
    app["adapter"].inject_message("你好，在吗？", sender_id="u1")
    app["sender"].pump()
    app["adapter"].stop()
    rows = app["latency_store"].read_all()
    sent = [r for r in rows if r.get("outcome") == "sent"]
    assert sent
    detect = sent[0]["segments"].get("detect_delay_s")
    assert detect == 0 or detect == 0.0


def test_announcement_has_no_latency_trace_by_default(tmp_path: Path, clock: FakeClock):
    """未附带 LatencyTrace 的裸 submit_announcement 不会凭空落盘。调度器路径见下一用例。"""
    store = LatencyStore(tmp_path / "latency.jsonl")
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply", latency_store=store)
    sender.submit_announcement("g1", "定时公告正文")
    sender.pump()
    assert adapter.sent_messages
    rows = store.read_all()
    assert rows == []


def test_scheduler_announcement_writes_latency_jsonl(tmp_path: Path, clock: FakeClock):
    """经调度器发出的定时公告应写入 latency.jsonl：含 run_mode、kind=announcement，不含正文。"""
    app = make_app(tmp_path, clock, mode="announce")
    anns = app["scheduler"].list_announcements()
    assert anns, "示例配置应至少有一条可触发的公告"
    body = anns[0].text
    app["scheduler"].fire_now(anns[0].id)
    app["sender"].pump()
    rows = app["latency_store"].read_all()
    sent = [r for r in rows if r.get("kind") in {"announcement", "announce"}]
    assert sent, f"定时公告应写入 latency.jsonl，实际 {rows}"
    rec = sent[0]
    assert rec.get("run_mode") in {"announce", "reply"}
    assert rec.get("kind") in {"announcement", "announce"}
    line = (tmp_path / "latency.jsonl").read_text(encoding="utf-8")
    assert body not in line
    assert body not in str(rec)


def test_dropped_observe_send_is_traced_when_pipeline_submits(tmp_path: Path, clock: FakeClock):
    app = make_app(tmp_path, clock, mode="observe")
    app["adapter"].start()
    app["adapter"].inject_message("你好，在吗？", sender_id="u1")
    app["sender"].pump()
    app["adapter"].stop()
    rows = app["latency_store"].read_all()
    assert rows, "observe 丢弃发送时仍应留下延迟记录"
    assert all(r.get("outcome") != "sent" for r in rows) or app["adapter"].sent_messages == []

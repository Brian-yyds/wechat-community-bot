# -*- coding: utf-8 -*-
"""全量抓取不漏消息：模拟库对账（CAP-01～CAP-04）。通过标准：漏抓 0、重复处理 0。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adapter import use_mock
from constants import CAPTURE_BURST_N, CAPTURE_BURST_WINDOW_SECONDS
from contracts.discovery import load_scheme_factory
from sim.reconcile import reconcile
from sim.wechat_db import SimulatedWeChatDB

SHANGHAI = timezone(timedelta(hours=8))


def _reader_factory():
    factory, source, reason = load_scheme_factory("local_db_reader")
    if factory is None:
        pytest.skip(reason)
    return factory


def _make_reader(factory, db: SimulatedWeChatDB, store=None):
    try:
        return factory(db_path=str(db.path), cursor_store=store if store is not None else {})
    except TypeError:
        try:
            return factory(str(db.path))
        except TypeError as exc:
            pytest.skip(f"LocalDbReader 构造签名无法适配: {exc}")


def _drain(reader, rounds: int = 20, limit: int = 200) -> list[str]:
    """连续拉取，返回按处理顺序的 msg_id 列表（用于计算重复处理）。"""
    processed: list[str] = []
    for _ in range(rounds):
        if hasattr(reader, "poll"):
            batch = reader.poll(limit=limit)
        else:
            batch = list(reader.iter_new())
        if not batch:
            break
        for m in batch:
            processed.append(str(m.get("id") or m.get("msg_id")))
    return processed


def test_cap01_reconcile_clean_ingest(tmp_path: Path):
    """CAP-01 正常顺序入库，漏抓率 0、重复处理 0。"""
    factory = _reader_factory()
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    start = datetime(2026, 11, 11, 13, 0, 0, tzinfo=SHANGHAI)
    expected = []
    for i in range(30):
        mid = f"ok-{i:03d}"
        db.insert(msg_id=mid, group_id="g1", sender_id="u1", content=f"正常消息{i}", ts=start + timedelta(seconds=i))
        expected.append(mid)
    reader = _make_reader(factory, db)
    processed = _drain(reader)
    report = reconcile(expected, processed)
    assert report.ok, (
        f"对账失败 miss={report.miss_rate:.2%} dup={report.dup_process_rate:.2%} "
        f"missed={report.missed_ids[:8]} dups={report.duplicate_ids[:8]}"
    )


def test_cap02_out_of_order_duplicates_and_concurrent(tmp_path: Path):
    """CAP-02 乱序时间戳、重复行、并发写入后对账仍为 0 漏抓 / 0 重复处理。"""
    factory = _reader_factory()
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    start = datetime(2026, 11, 11, 14, 0, 0, tzinfo=SHANGHAI)
    planned = []
    for i in range(80):
        ts = start + timedelta(seconds=i if i % 7 else max(0, i - 5))
        planned.append(
            {
                "msg_id": f"oo-{i:03d}",
                "group_id": "g1",
                "sender_id": f"u{i%5}",
                "content": f"并发乱序{i}",
                "ts": ts,
            }
        )
    db.write_concurrent(planned, workers=4)
    # 再写入若干重复行
    for i in (3, 17, 40):
        db.insert(msg_id=f"oo-{i:03d}", group_id="g1", sender_id="u0", content="重复行", ts=start)
    expected = [r["msg_id"] for r in planned]
    reader = _make_reader(factory, db)
    processed = _drain(reader)
    report = reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"漏抓 {report.missed_ids[:10]} rate={report.miss_rate}"
    assert report.dup_process_rate == 0.0, f"重复处理 {report.duplicate_ids[:10]} rate={report.dup_process_rate}"


def test_cap03_burst_500_in_one_minute(tmp_path: Path):
    """CAP-03 压测 burst：1 分钟窗口 500 条，对账漏抓 0、重复处理 0。"""
    factory = _reader_factory()
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    planned = db.seed_burst(n=CAPTURE_BURST_N, window_seconds=CAPTURE_BURST_WINDOW_SECONDS)
    expected = [r["msg_id"] for r in planned]
    assert len(set(expected)) == CAPTURE_BURST_N
    reader = _make_reader(factory, db)
    processed = _drain(reader, rounds=10, limit=200)
    report = reconcile(expected, processed)
    assert report.expected == CAPTURE_BURST_N
    assert report.miss_rate == 0.0, f"burst 漏抓 {len(report.missed_ids)} 条，例如 {report.missed_ids[:8]}"
    assert report.dup_process_rate == 0.0, f"burst 重复处理 {report.duplicate_ids[:8]}"


def test_cap04_lock_then_resume_no_loss(tmp_path: Path):
    """CAP-04 库锁期间允许本轮失败或重试，锁释放后补拉不得漏、不得重复处理。"""
    factory = _reader_factory()
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    start = datetime(2026, 11, 11, 15, 0, 0, tzinfo=SHANGHAI)
    expected = []
    for i in range(10):
        mid = f"lk-{i}"
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=f"锁测{i}", ts=start + timedelta(seconds=i))
        expected.append(mid)
    store = {}
    reader = _make_reader(factory, db, store)
    # 先读一部分
    first = reader.poll(limit=3) if hasattr(reader, "poll") else list(reader.iter_new())
    first_ids = [m.get("id") or m.get("msg_id") for m in first]
    try:
        with db.exclusive_lock(hold_seconds=0.12):
            try:
                reader.poll(limit=20)
            except Exception:  # 锁定错误可接受
                pass
    except Exception as exc:
        if "lock" in type(exc).__name__.lower() or "busy" in str(exc).lower():
            pass
        else:
            # 某些环境 BEGIN EXCLUSIVE 可能立即成功并释放，继续对账即可
            pass
    # 锁释放后用同一游标继续
    reader2 = _make_reader(factory, db, store)
    rest = _drain(reader2)
    processed = list(first_ids) + rest
    report = reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"锁后补拉漏抓 {report.missed_ids}"
    assert report.dup_process_rate == 0.0, f"锁后补拉重复处理 {report.duplicate_ids}"


def test_cap00_reconcile_helper_detects_errors():
    """对账脚本自身对「有漏 / 有重复」必须给出非零率（自检）。"""
    missed = reconcile(["a", "b", "c"], ["a", "c"])
    assert missed.miss_rate > 0 and "b" in missed.missed_ids
    dups = reconcile(["a", "b"], ["a", "a", "b"])
    assert dups.dup_process_rate > 0 and "a" in dups.duplicate_ids
    clean = reconcile(["a", "b"], ["b", "a"])
    assert clean.ok

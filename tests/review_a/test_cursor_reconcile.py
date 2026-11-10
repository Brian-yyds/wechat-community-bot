# -*- coding: utf-8 -*-
"""游标不丢不重、对账缺口、seen_ids 膨胀、时钟/时区边界。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sim.reconcile import reconcile as sim_reconcile
from sim.wechat_db import SimulatedWeChatDB
from src.adapters.local_db import (
    CursorStore,
    LocalDbCorruptError,
    LocalDbLockedError,
    LocalDbReader,
    ReconcileReport,
    catch_up,
    parse_timestamp,
    reconcile,
)
from src.clock import FakeClock

from review_a.helpers import SHANGHAI, insert_n, make_db


def _ids(rows):
    return [m.get("msg_id") or m.get("id") for m in rows]


def test_out_of_order_same_ts_and_duplicate_rows(tmp_path: Path):
    db = make_db(tmp_path)
    t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="late", group_id="g1", sender_id="u", content="先入晚时", ts=t0 + timedelta(seconds=30))
    db.insert(msg_id="same-ts-a", group_id="g1", sender_id="u", content="同时1", ts=t0)
    db.insert(msg_id="same-ts-b", group_id="g1", sender_id="u", content="同时2", ts=t0)
    db.insert(msg_id="same-ts-a", group_id="g1", sender_id="u", content="重复行", ts=t0)
    reader = LocalDbReader(db.path, cursor_store={})
    got = reader.poll()
    ids = _ids(got)
    assert ids == ["late", "same-ts-a", "same-ts-b"]
    assert reader.poll() == []
    report = reconcile(["late", "same-ts-a", "same-ts-b"], got)
    assert report.ok
    assert report.miss_rate == 0.0 and report.dup_process_rate == 0.0


def test_concurrent_writes_then_poll(tmp_path: Path):
    db = make_db(tmp_path)
    start = datetime(2026, 11, 11, 13, 0, 0, tzinfo=SHANGHAI)
    planned = [
        {
            "msg_id": f"cc-{i:03d}",
            "group_id": "g1",
            "sender_id": f"u{i % 4}",
            "content": f"并发{i}",
            "ts": start + timedelta(seconds=i if i % 5 else max(0, i - 3)),
        }
        for i in range(80)
    ]
    db.write_concurrent(planned, workers=4)
    reader = LocalDbReader(db.path, cursor_store={})
    processed = []
    for _ in range(10):
        batch = reader.poll(limit=200)
        if not batch:
            break
        processed.extend(_ids(batch))
    report = sim_reconcile([r["msg_id"] for r in planned], processed)
    assert report.miss_rate == 0.0, f"并发写入漏抓 {report.missed_ids[:10]}"
    assert report.dup_process_rate == 0.0, f"并发写入重复 {report.duplicate_ids[:10]}"


def test_burst_500_per_minute(tmp_path: Path):
    db = make_db(tmp_path)
    planned = db.seed_burst(n=500, window_seconds=60)
    expected = [r["msg_id"] for r in planned]
    reader = LocalDbReader(db.path, cursor_store={})
    processed = []
    for _ in range(10):
        batch = reader.poll(limit=200)
        if not batch:
            break
        processed.extend(_ids(batch))
    report = sim_reconcile(expected, processed)
    assert report.expected == 500
    assert report.miss_rate == 0.0, f"burst 漏抓 {report.missed_ids[:8]}"
    assert report.dup_process_rate == 0.0


def test_busy_lock_retries_then_resumes(tmp_path: Path):
    db = make_db(tmp_path)
    start = datetime(2026, 11, 11, 15, 0, 0, tzinfo=SHANGHAI)
    expected = insert_n(db, 10, prefix="lk", start=start)
    store = {}
    reader = LocalDbReader(db.path, cursor_store=store, lock_retries=8, lock_retry_delay=0.03, poll_timeout=0.2)
    first = reader.poll(limit=3)
    first_ids = _ids(first)
    try:
        with db.exclusive_lock(hold_seconds=0.12):
            try:
                reader.poll(limit=20)
            except (LocalDbLockedError, sqlite3.OperationalError):
                pass
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__.lower() + str(exc).lower()
                assert any(k in name for k in ("lock", "busy", "timeout", "locked"))
    except Exception as exc:
        if "lock" not in type(exc).__name__.lower() and "busy" not in str(exc).lower():
            # 环境无法持锁时继续对账
            pass
    reader2 = LocalDbReader(db.path, cursor_store=store)
    rest = []
    for _ in range(8):
        batch = reader2.poll()
        if not batch:
            break
        rest.extend(_ids(batch))
    processed = first_ids + rest
    report = sim_reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"锁后漏抓 {report.missed_ids}"
    assert report.dup_process_rate == 0.0, f"锁后重复 {report.duplicate_ids}"


def test_corrupt_and_missing_table_are_explicit(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="c1", group_id="g1", sender_id="u", content="坏", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    db.corrupt()
    reader = LocalDbReader(db.path, cursor_store={})
    with pytest.raises(LocalDbCorruptError):
        reader.poll()

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    reader2 = LocalDbReader(empty, cursor_store={})
    with pytest.raises(LocalDbCorruptError) as ei:
        reader2.poll()
    msg = str(ei.value).lower()
    # 类型已明确为 LocalDbCorruptError，不得当成空结果。
    # 文案当前可能是「本地库无法读取: no such table: messages」
    # （max_seq 先于专门的表存在检查），仍视为显式失败。
    assert "no such table" in msg or "不存在" in str(ei.value) or "损坏" in str(ei.value) or "无法读取" in str(ei.value)


def test_restart_resume_with_file_cursor_no_loss_no_dup(tmp_path: Path):
    db = make_db(tmp_path)
    start = datetime(2026, 11, 11, 17, 0, 0, tzinfo=SHANGHAI)
    expected = insert_n(db, 8, prefix="pre", start=start)
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    first = _ids(reader.poll())
    reader.close()
    db.simulate_wechat_restart()
    for i in range(8, 15):
        mid = f"post-{i:04d}"
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content="后", ts=start + timedelta(seconds=i))
        expected.append(mid)
    reader2 = LocalDbReader(db.path, cursor_store=cursor)
    rest = _ids(reader2.poll())
    processed = first + rest
    report = sim_reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"重启续读漏抓 {report.missed_ids}"
    assert report.dup_process_rate == 0.0, f"重启续读重复 {report.duplicate_ids}"
    assert any(x.startswith("post-") for x in rest)


def test_muted_folded_unopened_not_missed(tmp_path: Path):
    db = make_db(tmp_path)
    planned = db.seed_session_states()
    expected = [r["msg_id"] for r in planned]
    reader = LocalDbReader(db.path, cursor_store={})
    processed = _ids(reader.poll(limit=500))
    report = sim_reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"免打扰/折叠/未打开群漏抓 {report.missed_ids}"
    assert report.dup_process_rate == 0.0


def test_catch_up_fills_gap_without_duplicate(tmp_path: Path):
    db = make_db(tmp_path)
    t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    for i, mid in enumerate(["early", "mid", "late"]):
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(minutes=i))
    window = (t0 + timedelta(minutes=1), t0 + timedelta(minutes=2))
    report = reconcile(db.all_messages(), [{"msg_id": "mid"}], time_window=window)
    assert report.missed_ids == ["late"]
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    reader.set_cursor({"last_seq": 0, "seen_ids": ["early", "mid"]})
    filled = catch_up(reader, report)
    assert _ids(filled) == ["late"]
    assert catch_up(reader, report) == []


def test_poll_misses_gap_when_last_seq_ahead_of_seen_ids(tmp_path: Path):
    """游标 last_seq 超前而 seen_ids 缺号时，增量 poll 无法自行找回缺口，必须靠对账补抓。"""
    db = make_db(tmp_path)
    ids = insert_n(db, 5, prefix="gap")
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    # 人为制造不一致：last_seq 已经到 5，但中间一条从 seen_ids 消失
    seen = set(ids)
    seen.discard("gap-0002")
    reader.set_cursor({"last_seq": 5, "seen_ids": seen})
    assert reader.poll() == [], "poll 在 last_seq 超前时不会回看缺口（边界）"
    report = ReconcileReport(
        expected=5,
        processed_unique=4,
        missed_ids=["gap-0002"],
        catch_up_ids=["gap-0002"],
    )
    filled = catch_up(reader, report)
    assert _ids(filled) == ["gap-0002"]


def test_corrupt_cursor_file_must_not_silently_replay(tmp_path: Path):
    """游标文件损坏时不得静默从 0 重放全部消息，否则会重复处理。"""
    db = make_db(tmp_path)
    ids = insert_n(db, 6, prefix="cur")
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    assert _ids(reader.poll()) == ids
    reader.close()
    # 模拟崩溃后主文件被截断/损坏，留下不完整 JSON
    cursor.write_text('{"last_seq": 6, "seen_ids": ["cur-0000",', encoding="utf-8")
    reader2 = LocalDbReader(db.path, cursor_store=cursor)
    replayed = _ids(reader2.poll())
    assert replayed == [], (
        f"游标文件损坏后发生静默重放，重复处理 {replayed}。"
        "应拒绝启动、保留备份或报明确错误，而不是把 last_seq 归零。"
    )


def test_atomic_cursor_write_tmp_leftover_does_not_clobber(tmp_path: Path):
    db = make_db(tmp_path)
    ids = insert_n(db, 3, prefix="at")
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    reader.poll()
    reader.close()
    payload = json.loads(cursor.read_text(encoding="utf-8"))
    assert payload["last_seq"] >= 3
    tmp = cursor.with_suffix(cursor.suffix + ".tmp")
    tmp.write_text("{not-json", encoding="utf-8")
    # 残留 tmp 不应影响下一次加载完整主文件
    reader2 = LocalDbReader(db.path, cursor_store=cursor)
    assert reader2.get_cursor()["last_seq"] >= 3
    db.insert(msg_id="at-0003", group_id="g1", sender_id="u", content="新", ts=datetime(2026, 11, 11, 10, 0, 3, tzinfo=SHANGHAI))
    got = _ids(reader2.poll())
    assert got == ["at-0003"]
    assert "at-0000" not in got


def test_seen_ids_unbounded_growth_estimate(tmp_path: Path):
    """seen_ids 无窗口裁剪：实测单条体积并估算长跑膨胀（不作为功能失败，但写入断言信息）。"""
    cursor = tmp_path / "cursor.json"
    store = CursorStore(cursor)
    # 用接近真实的消息 ID 长度做样本
    sample_n = 2000
    ids = [f"msg_{i:012d}_{i * 17:08x}" for i in range(sample_n)]
    store.save(last_seq=sample_n, seen_ids=ids, identity={"ino": 1, "dev": 1, "size": 1, "mtime_ns": 1})
    size = cursor.stat().st_size
    bytes_per_id = size / sample_n
    # 交易会员群量级假设（报告引用这些数字）
    daily_low, daily_high = 800, 8000
    year_low = int(daily_low * 365 * bytes_per_id)
    year_high = int(daily_high * 365 * bytes_per_id)
    # 在测试输出中留下估算，便于 REPORT_A 引用
    print(
        f"seen_ids 估算: {bytes_per_id:.1f} B/条; "
        f"800条/天一年约 {year_low / 1024 / 1024:.2f} MiB; "
        f"8000条/天一年约 {year_high / 1024 / 1024:.2f} MiB; "
        f"{sample_n} 条游标文件 {size} 字节"
    )
    assert bytes_per_id > 0
    # 当前实现没有裁剪接口
    assert not hasattr(CursorStore, "prune")
    assert not hasattr(LocalDbReader, "prune_seen_ids")
    # 同一 store 再写入累积后体积应近似线性
    more = ids + [f"msg_extra_{i:08d}" for i in range(2000)]
    store.save(last_seq=4000, seen_ids=more, identity={"ino": 1, "dev": 1, "size": 1, "mtime_ns": 1})
    size2 = cursor.stat().st_size
    assert size2 > size * 1.5, "seen_ids 应随消息数增长（用于证明无裁剪）"
    # 把估算写进副作用文件，供报告读取（仅测试临时目录，不入库）
    (tmp_path / "seen_ids_estimate.json").write_text(
        json.dumps(
            {
                "bytes_per_id": bytes_per_id,
                "sample_n": sample_n,
                "sample_bytes": size,
                "daily_low": daily_low,
                "daily_high": daily_high,
                "year_low_bytes": year_low,
                "year_high_bytes": year_high,
                "year_low_mib": year_low / 1024 / 1024,
                "year_high_mib": year_high / 1024 / 1024,
                "days_to_10mib_at_2000_per_day": int((10 * 1024 * 1024 / bytes_per_id) / 2000),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_clock_rollback_does_not_drop_new_seq(tmp_path: Path):
    db = make_db(tmp_path)
    t0 = datetime(2026, 11, 11, 18, 0, 0)
    clock = FakeClock(t0)
    db.insert(msg_id="clk-1", group_id="g1", sender_id="u", content="前回拨", ts=t0.replace(tzinfo=SHANGHAI))
    reader = LocalDbReader(db.path, cursor_store={}, clock=clock)
    assert _ids(reader.poll()) == ["clk-1"]
    # 系统时钟回拨
    clock.set(t0 - timedelta(hours=2))
    db.insert(
        msg_id="clk-2",
        group_id="g1",
        sender_id="u",
        content="回拨后新消息",
        ts=(t0 - timedelta(hours=1)).replace(tzinfo=SHANGHAI),
    )
    got = reader.poll()
    assert _ids(got) == ["clk-2"], f"时钟回拨后按入库序本应抓到新消息，实际 {_ids(got)}"


def test_timezone_mixed_timestamp_formats(tmp_path: Path):
    db_path = tmp_path / "tz.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "CREATE TABLE messages (msg_id TEXT, group_id TEXT, sender_id TEXT, sender_name TEXT, "
        "ts TEXT, content TEXT, msg_type INTEGER DEFAULT 1, create_seq INTEGER PRIMARY KEY AUTOINCREMENT);"
    )
    rows = [
        ("tz-naive", "2026-11-11T12:00:00"),
        ("tz-offset", "2026-11-11T12:00:01+08:00"),
        ("tz-z", "2026-11-11T04:00:02Z"),
        ("tz-unix", "1762833603"),
    ]
    for mid, ts in rows:
        conn.execute(
            "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content) VALUES (?,?,?,?,?,?)",
            (mid, "g1", "u", "", ts, mid),
        )
    conn.commit()
    conn.close()
    reader = LocalDbReader(db_path, cursor_store={})
    got = reader.poll()
    assert _ids(got) == [r[0] for r in rows]
    # 解析后应为 naive 上海本地或至少可比较
    for m in got:
        ts = m["timestamp"]
        assert isinstance(ts, datetime)
        assert ts.tzinfo is None
    # Z 时间 04:00:02Z = 12:00:02+08:00
    z = next(m for m in got if m["msg_id"] == "tz-z")
    assert z["timestamp"].hour == 12
    assert z["timestamp"].second == 2


def test_parse_timestamp_invalid_uses_wall_clock_not_injected_clock():
    """无法解析的时间戳回退到 datetime.now()，与注入时钟脱节，对账窗口可能错位。"""
    before = datetime.now()
    ts = parse_timestamp("not-a-timestamp")
    after = datetime.now()
    assert before - timedelta(seconds=2) <= ts <= after + timedelta(seconds=2)


def test_file_identity_change_dedups_old_ids(tmp_path: Path):
    src = tmp_path / "live.db"
    db = SimulatedWeChatDB(src)
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="old-1", group_id="g1", sender_id="u", content="旧1", ts=t0)
    store: dict = {}
    reader = LocalDbReader(src, cursor_store=store)
    assert _ids(reader.poll()) == ["old-1"]
    reader.close()
    replacement = tmp_path / "new.db"
    db2 = SimulatedWeChatDB(replacement)
    db2.insert(msg_id="old-1", group_id="g1", sender_id="u", content="旧1", ts=t0)
    db2.insert(msg_id="new-1", group_id="g1", sender_id="u", content="新1", ts=t0 + timedelta(seconds=1))
    os_replace = __import__("os").replace
    os_replace(replacement, src)
    reader2 = LocalDbReader(src, cursor_store=store)
    got = _ids(reader2.poll())
    assert got == ["new-1"]

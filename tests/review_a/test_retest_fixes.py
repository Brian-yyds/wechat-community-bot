# -*- coding: utf-8 -*-
"""复测：对照首轮退回项逐条验证。不改 src/，不触碰仓库 data/PAUSE。"""
from __future__ import annotations

import json
import logging
import sqlite3
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.local_capture import DEFAULT_RECONCILE_INTERVAL, LocalCaptureAdapter
from src.adapters.local_db import (
    CursorCorruptError,
    CursorStore,
    EnvFileKeyProvider,
    KeyPermissionError,
    LocalDbReader,
    SqliteDbReader,
)
from src.clock import FakeClock
from src.logging_util import mask_config, redact

from review_a.helpers import SHANGHAI, file_meta, insert_n, make_db, pause_isolated, sha256_file


SECRET = "super-secret-db-key-value-9f3a"


def _ids(rows):
    return [m.get("msg_id") or m.get("id") for m in rows]


def test_corrupt_cursor_falls_back_to_backup_and_does_not_replay(tmp_path: Path):
    db = make_db(tmp_path)
    ids = insert_n(db, 6, prefix="cur")
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    assert _ids(reader.poll()) == ids
    reader.close()
    bak = cursor.with_suffix(cursor.suffix + ".bak")
    assert bak.is_file(), "首次写入后应留下 .bak 以便损坏时回退"
    bak_payload = json.loads(bak.read_text(encoding="utf-8"))
    assert bak_payload["last_seq"] >= 6
    cursor.write_text('{"last_seq": 6, "seen_ids": ["cur-0000",', encoding="utf-8")
    reader2 = LocalDbReader(db.path, cursor_store=cursor)
    replayed = _ids(reader2.poll())
    assert replayed == [], f"有备份时损坏主文件仍发生重放: {replayed}"
    isolated = list(tmp_path.glob("cursor.json.corrupt-*"))
    assert isolated, "损坏主游标应被隔离保存"
    # 回退后新消息仍可增量抓取
    db.insert(
        msg_id="cur-0006",
        group_id="g1",
        sender_id="u1",
        content="新",
        ts=datetime(2026, 11, 11, 10, 0, 6, tzinfo=SHANGHAI),
    )
    got = _ids(reader2.poll())
    assert got == ["cur-0006"]
    reader2.close()


def test_corrupt_cursor_without_backup_stops_and_does_not_zero(tmp_path: Path):
    db = make_db(tmp_path)
    insert_n(db, 3, prefix="nb")
    cursor = tmp_path / "cursor.json"
    cursor.write_text('{"last_seq": 3, "seen_ids": ["nb-0000",', encoding="utf-8")
    assert not (cursor.with_suffix(cursor.suffix + ".bak")).exists()
    with pytest.raises(CursorCorruptError, match="损坏"):
        LocalDbReader(db.path, cursor_store=cursor)
    isolated = list(tmp_path.glob("cursor.json.corrupt-*"))
    assert isolated, "无备份时损坏文件仍应隔离"
    # 不得留下可被当成全新游标的空主文件后静默重放
    if cursor.is_file():
        with pytest.raises((CursorCorruptError, json.JSONDecodeError)):
            # 若主文件仍在且仍损坏，再次打开必须继续报错，而不是归零。
            LocalDbReader(db.path, cursor_store=cursor)


def test_assert_readonly_writable_rejected_readonly_passes_no_leak(tmp_path: Path, monkeypatch):
    db = make_db(tmp_path)
    db.insert(msg_id="p0", group_id="g1", sender_id="u", content="探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    before = sha256_file(db.path)

    reader = LocalDbReader(db.path, cursor_store={})
    reader.assert_readonly()  # 默认只读 URI 应通过
    assert getattr(reader._reader, "_conn", None) is None
    after_ro = sha256_file(db.path)
    assert after_ro == before, "只读探测不得改写库文件"

    def _writable_connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(SqliteDbReader, "_connect", _writable_connect)
    reader2 = LocalDbReader(db.path, cursor_store={})
    with pytest.raises(AssertionError, match="只读"):
        reader2.assert_readonly()
    assert getattr(reader2._reader, "_conn", None) is None, "探测后不得泄漏连接"
    # CREATE TABLE 探测必须回滚，不得留下探测表
    conn = sqlite3.connect(str(db.path))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "_local_capture_ro_probe" not in tables
    assert sha256_file(db.path) == before
    reader.close()
    reader2.close()


def test_redact_covers_wechat_local_db_key_assignment():
    raw = f"启动失败 WECHAT_LOCAL_DB_KEY={SECRET} path=/tmp/key"
    out = redact(raw)
    assert SECRET not in out
    assert "WECHAT_LOCAL_DB_KEY=" in out or "wechat_local_db_key=" in out.lower()


def test_mask_config_hides_key_db_key_and_env_name():
    data = {
        "inbound": {
            "local_capture": {
                "db_path": "/tmp/decrypted.db",
                "key": SECRET,
                "db_key": SECRET,
                "WECHAT_LOCAL_DB_KEY": SECRET,
            }
        }
    }
    blob = str(mask_config(data))
    assert SECRET not in blob
    assert "***" in blob


def test_world_readable_key_file_warns_and_strict_rejects(tmp_path: Path, caplog):
    key_file = tmp_path / "db.key"
    key_file.write_text(SECRET, encoding="utf-8")
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    with caplog.at_level(logging.WARNING):
        key = EnvFileKeyProvider(env={}, key_file=key_file, strict=False).get_key()
    assert key == SECRET.encode("utf-8")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert any(k in logs for k in ("权限", "过宽", "chmod", "600")), logs
    with pytest.raises(KeyPermissionError, match="权限"):
        EnvFileKeyProvider(env={}, key_file=key_file, strict=True).get_key()
    with pytest.raises(KeyPermissionError):
        EnvFileKeyProvider(
            env={"WECHAT_LOCAL_DB_KEY_STRICT": "1"},
            key_file=key_file,
        ).get_key()


def test_trim_seen_window_does_not_reprocess_recent(tmp_path: Path):
    db = make_db(tmp_path)
    ids = insert_n(db, 40, prefix="w")
    reader = LocalDbReader(db.path, cursor_store={}, seen_ids_max=15, seen_seq_window=10)
    got = _ids(reader.poll(limit=200))
    assert got == ids
    seen = reader.get_cursor()["seen_ids"]
    assert len(seen) <= 15
    # 裁剪后不得把仍在窗口内的近期消息再处理一遍
    assert reader.poll() == []
    db.insert(
        msg_id="w-0040",
        group_id="g1",
        sender_id="u1",
        content="新",
        ts=datetime(2026, 11, 11, 10, 0, 40, tzinfo=SHANGHAI),
    )
    assert _ids(reader.poll()) == ["w-0040"]
    reader.close()


def test_file_replace_keeps_untrimmed_seen_ids(tmp_path: Path):
    """替换时去重集里还在的 ID 应保留（freeze_trim），不得重放。"""
    src = tmp_path / "live.db"
    db_live = make_db(tmp_path, name="live.db")
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    for mid in ["old-1", "old-2", "old-3"]:
        db_live.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0)
    store: dict = {}
    # 窗口足够大，替换前尚未裁掉历史 ID
    r1 = LocalDbReader(src, cursor_store=store, seen_ids_max=20000, seen_seq_window=10000)
    assert set(_ids(r1.poll())) == {"old-1", "old-2", "old-3"}
    r1.close()
    replacement = tmp_path / "new.db"
    db2 = make_db(tmp_path, name="new.db")
    for mid in ["old-1", "old-2", "old-3"]:
        db2.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0)
    db2.insert(msg_id="new-1", group_id="g1", sender_id="u", content="新", ts=t0 + timedelta(seconds=1))
    replacement.replace(src)
    r2 = LocalDbReader(src, cursor_store=store, seen_ids_max=20000, seen_seq_window=10000)
    got2 = _ids(r2.poll())
    assert got2 == ["new-1"], f"替换后应只出新消息，实际 {got2}"
    r2.close()


def test_already_trimmed_ids_are_not_replayed_after_file_replace(tmp_path: Path):
    """已被 trim 掉的 ID，库文件替换后不得重放；只放行水位线之后的新消息。"""
    src = tmp_path / "live.db"
    db_live = make_db(tmp_path, name="live.db")
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    for mid in ["old-1", "old-2", "old-3"]:
        db_live.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0)
    store: dict = {}
    r1 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    assert set(_ids(r1.poll())) == {"old-1", "old-2", "old-3"}
    cursor = r1.get_cursor()
    seen_after_trim = set(cursor["seen_ids"])
    assert "old-1" not in seen_after_trim, "前置：激进窗口已裁掉 oldest"
    assert int(cursor.get("watermark_seq") or 0) >= 3
    assert cursor.get("watermark_ts")
    r1.close()
    replacement = tmp_path / "new.db"
    db2 = make_db(tmp_path, name="new.db")
    for mid in ["old-1", "old-2", "old-3"]:
        db2.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0)
    db2.insert(msg_id="new-1", group_id="g1", sender_id="u", content="新", ts=t0 + timedelta(seconds=1))
    replacement.replace(src)
    r2 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    got2 = _ids(r2.poll())
    assert got2 == ["new-1"], f"库替换后应只出新消息、不得重放已裁 ID，实际 {got2}"
    r2.close()


def test_trim_keeps_watermark_and_timestamp_latch(tmp_path: Path):
    """裁剪只收缩 seen_ids，水位线（序号 + 时间戳）不得回退。"""
    db = make_db(tmp_path)
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    ids = insert_n(db, 12, prefix="wm", start=t0)
    reader = LocalDbReader(db.path, cursor_store={}, seen_ids_max=3, seen_seq_window=2)
    assert _ids(reader.poll(limit=200)) == ids
    cursor = reader.get_cursor()
    seen = list(cursor["seen_ids"])
    assert len(seen) <= 3
    assert int(cursor.get("watermark_seq") or 0) >= 12
    assert cursor.get("watermark_ts")
    # 再次裁剪不得降低水位线
    before_seq = int(cursor["watermark_seq"])
    before_ts = cursor["watermark_ts"]
    reader.trim_seen_window()
    after = reader.get_cursor()
    assert int(after["watermark_seq"]) >= before_seq
    assert after["watermark_ts"] == before_ts
    assert reader.poll() == []
    reader.close()


def test_file_replace_does_not_replay_history_before_watermark(tmp_path: Path):
    """库文件替换后，水位线之前（含相等时间戳）的未见历史不得重放。"""
    src = tmp_path / "live.db"
    db_live = make_db(tmp_path, name="live.db")
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    old_ids = [f"hist-{i:04d}" for i in range(8)]
    for i, mid in enumerate(old_ids):
        db_live.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    store: dict = {}
    r1 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    assert _ids(r1.poll(limit=200)) == old_ids
    watermark_ts = r1.get_cursor().get("watermark_ts")
    assert watermark_ts
    r1.close()

    replacement = tmp_path / "new.db"
    db2 = make_db(tmp_path, name="new.db")
    for i, mid in enumerate(old_ids):
        db2.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    # 与水位线相同时间戳的「漏网」历史，以及更早的幽灵消息，都不得重放。
    db2.insert(msg_id="ghost-same-ts", group_id="g1", sender_id="u", content="同戳", ts=t0 + timedelta(seconds=7))
    db2.insert(msg_id="ghost-earlier", group_id="g1", sender_id="u", content="更早", ts=t0 + timedelta(seconds=1))
    replacement.replace(src)
    r2 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    got2 = _ids(r2.poll(limit=200))
    assert got2 == [], f"水位线之前的历史不得重放，实际 {got2}"
    filled = r2.reconcile_recent(seq_back=10000, limit=5000)
    assert _ids(filled) == [], f"对账补抓也不得重放水位线之前的消息，实际 {_ids(filled)}"
    r2.close()


def test_messages_after_watermark_are_not_missed_after_replace(tmp_path: Path):
    """库替换后，水位线之后的新消息必须被抓到，不得漏。"""
    src = tmp_path / "live.db"
    db_live = make_db(tmp_path, name="live.db")
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    for i, mid in enumerate(["old-a", "old-b", "old-c"]):
        db_live.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    store: dict = {}
    r1 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    assert set(_ids(r1.poll())) == {"old-a", "old-b", "old-c"}
    r1.close()

    replacement = tmp_path / "new.db"
    db2 = make_db(tmp_path, name="new.db")
    for i, mid in enumerate(["old-a", "old-b", "old-c"]):
        db2.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    db2.insert(msg_id="fresh-1", group_id="g1", sender_id="u", content="新1", ts=t0 + timedelta(seconds=30))
    db2.insert(msg_id="fresh-2", group_id="g1", sender_id="u", content="新2", ts=t0 + timedelta(seconds=31))
    replacement.replace(src)
    r2 = LocalDbReader(src, cursor_store=store, seen_ids_max=2, seen_seq_window=1)
    got2 = _ids(r2.poll(limit=200))
    assert got2 == ["fresh-1", "fresh-2"], f"水位线之后的新消息不应漏抓，实际 {got2}"
    r2.close()


def test_idle_poll_skips_rewriting_cursor_file(tmp_path: Path):
    db = make_db(tmp_path)
    insert_n(db, 20, prefix="io")
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    reader.poll()
    assert cursor.is_file()
    time.sleep(0.05)
    before = file_meta(cursor)
    assert reader.poll() == []
    after = file_meta(cursor)
    assert after["sha256"] == before["sha256"]
    assert after["mtime_ns"] == before["mtime_ns"], "无新消息时不应重写整份游标 JSON"
    reader.close()


def test_reconcile_recent_fills_gap_but_poll_stays_incremental(tmp_path: Path):
    db = make_db(tmp_path)
    ids = insert_n(db, 5, prefix="gap")
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    seen = set(ids)
    seen.discard("gap-0002")
    reader.set_cursor({"last_seq": 5, "seen_ids": seen})
    assert reader.poll() == [], "poll 应保持增量语义，不因缺号自行回看"
    filled = reader.reconcile_recent(seq_back=10)
    assert _ids(filled) == ["gap-0002"]
    assert reader.reconcile_recent(seq_back=10) == []
    reader.close()


def test_adapter_reconcile_every_60s_and_not_sooner(tmp_path: Path):
    assert DEFAULT_RECONCILE_INTERVAL == 60.0
    db = make_db(tmp_path)
    ids = insert_n(db, 5, prefix="r60")
    clock = FakeClock(datetime(2026, 11, 11, 12, 0, 0))
    store: dict = {}
    adapter = LocalCaptureAdapter(
        db.path,
        cursor_store=store,
        clock=clock,
        reconcile_interval_seconds=DEFAULT_RECONCILE_INTERVAL,
    )
    first = [m.message_id for m in adapter.poll_once()]
    assert first == ids
    # 制造缺口
    reader = adapter._reader
    seen = set(ids)
    seen.discard("r60-0002")
    reader.set_cursor({"last_seq": 5, "seen_ids": seen})
    # 刚对账过后立即再 poll：增量仍为空，且未到 60s 不得补抓
    assert [m.message_id for m in adapter.poll_once()] == []
    clock.set(datetime(2026, 11, 11, 12, 0, 59))
    assert [m.message_id for m in adapter.poll_once()] == []
    clock.set(datetime(2026, 11, 11, 12, 1, 0))
    filled = [m.message_id for m in adapter.poll_once()]
    assert filled == ["r60-0002"], f"满 60s 应对账补回缺口，实际 {filled}"
    assert [m.message_id for m in adapter.poll_once()] == []
    adapter._reader.close()


def test_regression_hash_unchanged_and_zero_miss_dup(tmp_path: Path):
    db = make_db(tmp_path)
    planned = db.seed_burst(n=80, window_seconds=60)
    # 叠加免打扰/折叠/未打开群与重复行
    muted = db.seed_session_states()
    expected = [r["msg_id"] for r in planned] + [r["msg_id"] for r in muted]
    before = sha256_file(db.path)
    reader = LocalDbReader(db.path, cursor_store={})
    processed = []
    for _ in range(8):
        batch = _ids(reader.poll(limit=200))
        if not batch:
            break
        processed.extend(batch)
    reader.close()
    after = sha256_file(db.path)
    assert after == before, "只读前后库文件哈希必须不变"
    from sim.reconcile import reconcile as sim_reconcile

    report = sim_reconcile(expected, processed)
    assert report.miss_rate == 0.0, f"漏抓 {report.missed_ids[:8]}"
    assert report.dup_process_rate == 0.0, f"重复 {report.duplicate_ids[:8]}"

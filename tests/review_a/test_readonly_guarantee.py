# -*- coding: utf-8 -*-
"""只读保证：mode=ro、写入失败、哈希/mtime 不变、不额外写库、不加长时锁。"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from src.adapters.local_db import LocalDbReader, SqliteDbReader, readonly_sqlite_uri

from review_a.helpers import SHANGHAI, file_meta, make_db, sidecar_files, sha256_file


def test_readonly_uri_uses_mode_ro(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="r0", group_id="g1", sender_id="u", content="x", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    uri = readonly_sqlite_uri(db.path)
    assert "mode=ro" in uri
    assert uri.startswith("file:")


def test_write_via_readonly_uri_fails(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="r1", group_id="g1", sender_id="u", content="只读探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    uri = readonly_sqlite_uri(db.path)
    conn = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "INSERT INTO messages(msg_id, group_id, sender_id, ts, content) VALUES (?,?,?,?,?)",
                ("probe", "g1", "u", "2026-11-11T00:00:00", "should-fail"),
            )
            conn.commit()
    finally:
        conn.close()
    ids = {r["msg_id"] for r in db.all_messages()}
    assert "probe" not in ids


def test_poll_does_not_change_hash_or_mtime(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="h1", group_id="g1", sender_id="u", content="哈希", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    before = file_meta(db.path)
    before_sidecars = sidecar_files(db.path)
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    reader.close()
    after = file_meta(db.path)
    after_sidecars = sidecar_files(db.path)
    assert after["sha256"] == before["sha256"], "只读拉取后主库内容哈希发生变化"
    assert after["size"] == before["size"]
    assert after["mtime_ns"] == before["mtime_ns"], "只读拉取后主库 mtime 发生变化"
    created = after_sidecars - before_sidecars
    unexpected = {n for n in created if not n.endswith(("-wal", "-shm"))}
    assert not unexpected, f"只读打开不应创建 -wal/-shm 以外的旁路文件: {unexpected}"


def test_delete_journal_db_does_not_create_wal_shm(tmp_path: Path):
    """默认 DELETE 日志模式下，只读打开不应创建 -wal/-shm。若出现则需说明原因。"""
    db = make_db(tmp_path)
    conn = sqlite3.connect(str(db.path))
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    conn.close()
    assert str(mode).lower() in {"delete", "off", "memory", "truncate", "persist"}
    db.insert(msg_id="w1", group_id="g1", sender_id="u", content="无wal", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    before = sidecar_files(db.path)
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    reader.close()
    after = sidecar_files(db.path)
    created = after - before
    wal_like = {n for n in created if n.endswith(("-wal", "-shm", "-journal"))}
    assert not wal_like, f"DELETE 日志库被只读打开后出现日志旁路文件: {wal_like}"


def test_wal_mode_readonly_does_not_rewrite_main_db(tmp_path: Path):
    """WAL 模式下只读打开：主库哈希不应被改写；若产生/改动 -wal/-shm，在报告中说明。"""
    db_path = tmp_path / "wal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        "CREATE TABLE messages (msg_id TEXT, group_id TEXT, sender_id TEXT, sender_name TEXT, "
        "ts TEXT, content TEXT, msg_type INTEGER DEFAULT 1, create_seq INTEGER PRIMARY KEY AUTOINCREMENT);"
    )
    conn.execute(
        "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content) VALUES (?,?,?,?,?,?)",
        ("wal-1", "g1", "u", "", "2026-11-11T10:00:00+08:00", "wal消息"),
    )
    conn.commit()
    conn.close()
    before = file_meta(db_path)
    before_sidecars = {p.name: file_meta(p) if p.is_file() else None for p in db_path.parent.iterdir() if p.name.startswith(db_path.name)}
    reader = LocalDbReader(db_path, cursor_store={})
    got = reader.poll()
    reader.close()
    assert [m["msg_id"] for m in got] == ["wal-1"]
    after = file_meta(db_path)
    assert after["sha256"] == before["sha256"], "WAL 库只读打开后主库内容被改写"
    # 允许既有 -wal/-shm 被读取；不允许新增非 wal/shm 文件。
    after_names = sidecar_files(db_path)
    unexpected = {n for n in after_names if not n.endswith(("-wal", "-shm"))}
    assert not unexpected, f"出现非 wal/shm 旁路文件: {unexpected}"


def test_assert_readonly_leaves_or_releases_connection(tmp_path: Path):
    """assert_readonly 之后不应长时间占用连接；下一次写入应能迅速拿到锁。"""
    db = make_db(tmp_path)
    db.insert(msg_id="l0", group_id="g1", sender_id="u", content="锁", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = LocalDbReader(db.path, cursor_store={})
    reader.assert_readonly()
    inner = reader._reader
    # 记录是否泄漏连接（供报告引用；泄漏本身在下一断言中体现为写端被挡）。
    leaked = getattr(inner, "_conn", None) is not None
    t0 = time.monotonic()
    db.insert(msg_id="l1", group_id="g1", sender_id="u", content="锁后写入", ts=datetime(2026, 11, 11, 0, 0, 1, tzinfo=SHANGHAI))
    elapsed = time.monotonic() - t0
    reader.close()
    assert elapsed < 1.0, (
        f"assert_readonly 之后写入被挡住 {elapsed:.2f}s，可能持有只读连接未释放（leaked={leaked}）"
    )


def test_poll_does_not_hold_long_lock_against_writer(tmp_path: Path):
    db = make_db(tmp_path)
    db.insert(msg_id="k0", group_id="g1", sender_id="u", content="前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    t0 = time.monotonic()
    db.insert(msg_id="k1", group_id="g1", sender_id="u", content="后", ts=datetime(2026, 11, 11, 0, 0, 1, tzinfo=SHANGHAI))
    elapsed = time.monotonic() - t0
    got = reader.poll()
    reader.close()
    assert [m["msg_id"] for m in got] == ["k1"]
    assert elapsed < 0.5, f"轮询后写入被挡住 {elapsed:.2f}s，可能持有长时锁"


def test_assert_readonly_detects_writable_connection(tmp_path: Path, monkeypatch):
    """assert_readonly 必须在连接其实可写时失败。若 INSERT 因缺列/非空约束失败而被当成只读，则本用例失败。"""
    db = make_db(tmp_path)
    db.insert(msg_id="p0", group_id="g1", sender_id="u", content="探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))

    def _writable_connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(SqliteDbReader, "_connect", _writable_connect)
    reader = LocalDbReader(db.path, cursor_store={})
    with pytest.raises(AssertionError, match="只读"):
        reader.assert_readonly()
    reader.close()
    ids = {r["msg_id"] for r in db.all_messages()}
    assert "probe" not in ids

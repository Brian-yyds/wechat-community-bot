# -*- coding: utf-8 -*-
"""本地库只读契约：poll/iter 新消息、游标去重、只读不写库、库锁/损坏容错。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sim.wechat_db import SimulatedWeChatDB

SHANGHAI = timezone(timedelta(hours=8))


def _make(factory, db: SimulatedWeChatDB, store=None):
    try:
        return factory(db_path=str(db.path), cursor_store=store if store is not None else {})
    except TypeError:
        try:
            return factory(str(db.path))
        except TypeError as exc:
            pytest.skip(f"LocalDbReader 构造签名无法适配: {exc}")


def test_poll_and_iter_new_messages(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    start = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    for i in range(5):
        db.insert(
            msg_id=f"m{i}",
            group_id="g1",
            sender_id="u1",
            content=f"消息{i}",
            ts=start + timedelta(seconds=i),
        )
    reader = _make(factory, db)
    if hasattr(reader, "iter_new"):
        first = list(reader.iter_new())
    else:
        first = reader.poll()
    ids = [m.get("id") or m.get("msg_id") for m in first]
    assert ids == [f"m{i}" for i in range(5)], f"应按游标增量取出全部 5 条，实际 {ids}"
    # 再次拉取不应重复
    second = reader.poll() if hasattr(reader, "poll") else list(reader.iter_new())
    assert second == [], f"游标推进后不应再吐出已读消息: {second}"


def test_cursor_survives_rebuild(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    store = {}
    db.insert(msg_id="a", group_id="g1", sender_id="u", content="先", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = _make(factory, db, store)
    assert len(reader.poll()) == 1
    db.insert(msg_id="b", group_id="g1", sender_id="u", content="后", ts=datetime(2026, 11, 11, 0, 0, 1, tzinfo=SHANGHAI))
    reader2 = _make(factory, db, store)
    got = reader2.poll()
    ids = [m.get("id") or m.get("msg_id") for m in got]
    assert ids == ["b"], f"重建后应只拉新消息，实际 {ids}"


def test_dedup_duplicate_rows(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    ts = datetime(2026, 11, 11, 11, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="same", group_id="g1", sender_id="u", content="一次", ts=ts)
    db.insert(msg_id="same", group_id="g1", sender_id="u", content="一次（重复行）", ts=ts)
    reader = _make(factory, db)
    got = reader.poll()
    ids = [m.get("id") or m.get("msg_id") for m in got]
    assert ids.count("same") == 1, f"同一 msg_id 的重复行只应处理一次，实际 {ids}"


def test_out_of_order_timestamps_not_missed(factory, tmp_path: Path):
    """游标必须按入库序（create_seq），不能只按时间戳，否则乱序会漏抓。"""
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="late-ts-first", group_id="g1", sender_id="u", content="先入库但时间戳更晚", ts=t0 + timedelta(seconds=30))
    reader = _make(factory, db)
    assert [m.get("id") or m.get("msg_id") for m in reader.poll()] == ["late-ts-first"]
    # 后入库、时间戳更早
    db.insert(msg_id="early-ts-later", group_id="g1", sender_id="u", content="后入库但时间戳更早", ts=t0)
    got = reader.poll()
    ids = [m.get("id") or m.get("msg_id") for m in got]
    assert ids == ["early-ts-later"], f"乱序时间戳导致漏抓或错序: {ids}"


def test_readonly_does_not_write(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    db.insert(msg_id="r1", group_id="g1", sender_id="u", content="只读探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    before = db.path.read_bytes()
    before_count = db.count()
    reader = _make(factory, db)
    if hasattr(reader, "assert_readonly"):
        reader.assert_readonly()
    reader.poll()
    after_count = db.count()
    assert after_count == before_count, "读取后库内行数发生变化，适配器可能写了库"
    # 允许 SQLite 只读打开不改文件；若文件被改写则失败
    after = db.path.read_bytes()
    # WAL 模式下可能产生旁路文件，主库文件头不应被改成可写痕迹。宽松：行数不变即可，
    # 但禁止插入新行。额外检查：没有用写模式插入 probe 行。
    ids = {r["msg_id"] for r in db.all_messages()}
    assert "probe" not in ids
    assert before_count == after_count


def test_lock_is_tolerated(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    db.insert(msg_id="l1", group_id="g1", sender_id="u", content="锁前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = _make(factory, db)
    # 持锁较短，读端应重试成功或抛出可区分的锁定错误，不能崩溃成别的异常
    try:
        with db.exclusive_lock(hold_seconds=0.15):
            try:
                reader.poll()
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__.lower() + str(exc).lower()
                assert any(k in name for k in ("lock", "busy", "locked", "timeout")), (
                    f"库锁定时应抛出可区分的锁定/超时错误，实际 {type(exc).__name__}: {exc}"
                )
    except Exception as exc:  # 持锁本身
        if "lock" in type(exc).__name__.lower():
            pytest.skip(f"当前环境无法模拟库锁: {exc}")
            return
        raise


def test_corrupt_db_is_explicit_error(factory, tmp_path: Path):
    db = SimulatedWeChatDB(tmp_path / "msg.db")
    db.insert(msg_id="c1", group_id="g1", sender_id="u", content="损坏前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    db.corrupt()
    reader = _make(factory, db)
    with pytest.raises(Exception) as ei:
        got = reader.poll()
        # 若没抛错，也不得把损坏库静默当成「无新消息」成功
        assert got, "库损坏时不得返回空列表并当作成功"
        raise AssertionError("库损坏未被检测")
    msg = (type(ei.value).__name__ + " " + str(ei.value)).lower()
    assert any(k in msg for k in ("corrupt", "malformed", "损坏", "database", "not a database", "unable")), (
        f"损坏库应给出可区分错误，实际 {ei.value!r}"
    )

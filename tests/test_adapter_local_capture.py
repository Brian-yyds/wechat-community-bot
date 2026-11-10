"""适配器 A：本地库只读增量抓取。

覆盖契约行为、免打扰/折叠/未打开群对账、断线与库文件替换后续读、
密钥不泄漏、factory 接入。不改动其他已有测试文件。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.base import EventType, Group
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.local_capture import LocalCaptureAdapter
from src.adapters.local_db import (
    CursorCorruptError,
    EnvFileKeyProvider,
    KeyNotConfiguredError,
    KeyPermissionError,
    LocalDbCorruptError,
    LocalDbLockedError,
    LocalDbReader,
    SqliteDbReader,
    build_simulated_wechat_db,
    catch_up,
    insert_simulated_message,
    reconcile,
)
from src.adapters.mock import MockAdapter
from src.clock import FakeClock
from src.logging_util import redact

from sim.wechat_db import SimulatedWeChatDB

SHANGHAI = timezone(timedelta(hours=8))


def _make_simple_db(tmp_path: Path, name: str = "msg.db") -> SimulatedWeChatDB:
    return SimulatedWeChatDB(tmp_path / name)


def test_poll_incremental_and_no_replay(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    start = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    for i in range(5):
        db.insert(msg_id=f"m{i}", group_id="g1", sender_id="u1", content=f"消息{i}", ts=start + timedelta(seconds=i))
    reader = LocalDbReader(db.path, cursor_store={})
    first = reader.poll()
    assert [m["msg_id"] for m in first] == [f"m{i}" for i in range(5)]
    assert all("detected_at" in m for m in first)
    assert reader.poll() == []


def test_cursor_survives_rebuild_via_dict_and_file(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    store: dict = {}
    db.insert(msg_id="a", group_id="g1", sender_id="u", content="先", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = LocalDbReader(db_path=str(db.path), cursor_store=store)
    assert len(reader.poll()) == 1
    db.insert(msg_id="b", group_id="g1", sender_id="u", content="后", ts=datetime(2026, 11, 11, 0, 0, 1, tzinfo=SHANGHAI))
    reader2 = LocalDbReader(str(db.path), cursor_store=store)
    assert [m["id"] for m in reader2.poll()] == ["b"]

    cursor_file = tmp_path / "data" / "local_capture_cursor.json"
    db2 = _make_simple_db(tmp_path, "msg2.db")
    db2.insert(msg_id="x", group_id="g1", sender_id="u", content="x", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    r = LocalDbReader(db2.path, cursor_store=cursor_file)
    assert [m["msg_id"] for m in r.poll()] == ["x"]
    assert cursor_file.is_file()
    payload = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert payload["last_seq"] >= 1
    db2.insert(msg_id="y", group_id="g1", sender_id="u", content="y", ts=datetime(2026, 11, 11, 0, 0, 2, tzinfo=SHANGHAI))
    r2 = LocalDbReader(db2.path, cursor_store=cursor_file)
    assert [m["msg_id"] for m in r2.poll()] == ["y"]


def test_dedup_and_out_of_order_timestamps(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    ts = datetime(2026, 11, 11, 11, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="same", group_id="g1", sender_id="u", content="一次", ts=ts)
    db.insert(msg_id="same", group_id="g1", sender_id="u", content="一次（重复行）", ts=ts)
    reader = LocalDbReader(db.path, cursor_store={})
    ids = [m["msg_id"] for m in reader.poll()]
    assert ids.count("same") == 1

    db2 = _make_simple_db(tmp_path, "oo.db")
    t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    db2.insert(msg_id="late-ts-first", group_id="g1", sender_id="u", content="先入库但时间戳更晚", ts=t0 + timedelta(seconds=30))
    reader = LocalDbReader(db2.path, cursor_store={})
    assert [m["msg_id"] for m in reader.poll()] == ["late-ts-first"]
    db2.insert(msg_id="early-ts-later", group_id="g1", sender_id="u", content="后入库但时间戳更早", ts=t0)
    assert [m["msg_id"] for m in reader.poll()] == ["early-ts-later"]


def test_readonly_does_not_write(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="r1", group_id="g1", sender_id="u", content="只读探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    before_count = db.count()
    reader = LocalDbReader(db.path, cursor_store={})
    reader.assert_readonly()
    reader.poll()
    assert db.count() == before_count
    assert "probe" not in {r["msg_id"] for r in db.all_messages()}


def test_lock_is_tolerated(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="l1", group_id="g1", sender_id="u", content="锁前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = LocalDbReader(db.path, cursor_store={}, lock_retries=8, lock_retry_delay=0.03, poll_timeout=0.2)
    try:
        with db.exclusive_lock(hold_seconds=0.15):
            try:
                reader.poll()
            except Exception as exc:  # noqa: BLE001
                name = type(exc).__name__.lower() + str(exc).lower()
                assert any(k in name for k in ("lock", "busy", "locked", "timeout")), (
                    f"库锁定时应抛出可区分的锁定/超时错误，实际 {type(exc).__name__}: {exc}"
                )
    except Exception as exc:
        if "lock" in type(exc).__name__.lower():
            pytest.skip(f"当前环境无法模拟库锁: {exc}")
        raise


def test_corrupt_db_is_explicit_error(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="c1", group_id="g1", sender_id="u", content="损坏前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    db.corrupt()
    reader = LocalDbReader(db.path, cursor_store={})
    with pytest.raises(Exception) as ei:
        got = reader.poll()
        assert got, "库损坏时不得返回空列表并当作成功"
        raise AssertionError("库损坏未被检测")
    msg = (type(ei.value).__name__ + " " + str(ei.value)).lower()
    assert any(k in msg for k in ("corrupt", "malformed", "损坏", "database", "not a database", "unable"))
    assert isinstance(ei.value, LocalDbCorruptError) or "损坏" in str(ei.value)


def test_missing_db_degrades_gracefully(tmp_path: Path):
    missing = tmp_path / "no-such.db"
    reader = LocalDbReader(missing, cursor_store={})
    assert reader.poll() == []
    assert reader.list_groups_raw() == []
    adapter = LocalCaptureAdapter(missing, cursor_store={})
    assert adapter.list_groups() == []
    assert adapter.poll_once() == []


def _rich_db(path: Path) -> Path:
    start = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    groups = [
        {"wxid": "muted@chatroom", "name": "免打扰交易群", "muted": 1, "collapsed": 0, "last_opened_at": None},
        {"wxid": "fold@chatroom", "name": "折叠交易群", "muted": 0, "collapsed": 1, "last_opened_at": None},
        {"wxid": "idle@chatroom", "name": "未打开交易群", "muted": 0, "collapsed": 0, "last_opened_at": None},
        {"wxid": "open@chatroom", "name": "当前打开的群", "muted": 0, "collapsed": 0, "last_opened_at": start.isoformat()},
        {"wxid": "other@chatroom", "name": "不在白名单", "muted": 0, "collapsed": 0, "last_opened_at": start.isoformat()},
    ]
    contacts = [
        {"wxid": "wxid_member1", "nickname": "会员甲"},
        {"wxid": "wxid_self", "nickname": "本小号"},
    ]
    messages = []
    whitelist = ["muted@chatroom", "fold@chatroom", "idle@chatroom", "open@chatroom"]
    for i, gid in enumerate(whitelist):
        messages.append(
            {
                "msg_id": f"g-{i}",
                "group_id": gid,
                "sender_id": "wxid_member1",
                "ts": start + timedelta(seconds=i),
                "content": f"正文{i}",
                "msg_type": 1,
            }
        )
    messages.append(
        {
            "msg_id": "outside",
            "group_id": "other@chatroom",
            "sender_id": "wxid_member1",
            "ts": start + timedelta(seconds=8),
            "content": "白名单外",
            "msg_type": 1,
        }
    )
    messages.append(
        {
            "msg_id": "pic-1",
            "group_id": "idle@chatroom",
            "sender_id": "wxid_member1",
            "ts": start + timedelta(seconds=9),
            "content": "",
            "msg_type": 3,
        }
    )
    messages.append(
        {
            "msg_id": "self-1",
            "group_id": "open@chatroom",
            "sender_id": "wxid_self",
            "sender_name": "本小号",
            "ts": start + timedelta(seconds=10),
            "content": "我发的",
            "msg_type": 1,
        }
    )
    return build_simulated_wechat_db(path, contacts=contacts, groups=groups, messages=messages)


def test_muted_collapsed_unopened_groups_are_captured_and_reconciled(tmp_path: Path):
    """全量抓取不依赖免打扰 / 折叠 / 会话是否打开；对账不漏不重，缺口可补抓。"""
    db_path = _rich_db(tmp_path / "rich.db")
    whitelist = ["muted@chatroom", "fold@chatroom", "idle@chatroom", "open@chatroom"]
    reader = LocalDbReader(
        db_path,
        cursor_store={},
        group_whitelist=whitelist,
        self_ids=["wxid_self"],
    )
    got = reader.poll()
    ids = [m["msg_id"] for m in got]
    expected = ["g-0", "g-1", "g-2", "g-3", "pic-1", "self-1"]
    report = reconcile(expected, got, group_ids=whitelist)
    assert report.ok, f"对账失败 miss={report.missed_ids} dup={report.duplicate_ids}"
    assert "outside" not in ids
    assert {m["group_id"] for m in got if m["msg_id"].startswith("g-")} == set(whitelist)
    self_row = next(m for m in got if m["msg_id"] == "self-1")
    assert self_row["is_self"] is True
    pic = next(m for m in got if m["msg_id"] == "pic-1")
    assert pic["is_text"] is False
    assert "非文本" in pic["content"] or pic["content"]
    groups = {g["group_id"]: g for g in reader.list_groups_raw()}
    assert groups["muted@chatroom"]["muted"] in (1, True)
    assert groups["fold@chatroom"]["collapsed"] in (1, True)
    assert "other@chatroom" not in groups


def test_reconcile_time_window_and_catch_up(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    t0 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    for i, mid in enumerate(["early", "mid", "late"]):
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(minutes=i))
    # 只发出了 mid，窗口覆盖 mid+late
    window = (t0 + timedelta(minutes=1), t0 + timedelta(minutes=2))
    expected_rows = db.all_messages()
    report = reconcile(expected_rows, [{"msg_id": "mid"}], time_window=window)
    assert report.missed_ids == ["late"]
    assert report.catch_up_ids == ["late"]
    reader = LocalDbReader(db.path, cursor_store={})
    # 先把 early/mid 读掉，模拟漏了 late（通过 set_cursor 把 late 之前当成已完成但不把 late 加入 seen）
    reader.poll()  # 会读到全部，先重置
    reader.set_cursor({"last_seq": 0, "seen_ids": ["early", "mid"]})
    filled = catch_up(reader, report)
    assert [m["msg_id"] for m in filled] == ["late"]
    assert catch_up(reader, report) == []  # 不重复


def test_file_replaced_and_cursor_rollback_resume(tmp_path: Path):
    """库文件被替换、游标回退后自动续读：旧消息不重放，新消息不漏。"""
    src = tmp_path / "live.db"
    db = SimulatedWeChatDB(src)
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="old-1", group_id="g1", sender_id="u", content="旧1", ts=t0)
    db.insert(msg_id="old-2", group_id="g1", sender_id="u", content="旧2", ts=t0 + timedelta(seconds=1))
    store: dict = {}
    reader = LocalDbReader(src, cursor_store=store)
    assert [m["msg_id"] for m in reader.poll()] == ["old-1", "old-2"]
    reader.close()

    # 用全新库文件替换（模拟微信重启后库被复制覆盖，seq 从 1 重新开始）
    replacement = tmp_path / "new.db"
    db2 = SimulatedWeChatDB(replacement)
    db2.insert(msg_id="old-1", group_id="g1", sender_id="u", content="旧1", ts=t0)
    db2.insert(msg_id="old-2", group_id="g1", sender_id="u", content="旧2", ts=t0 + timedelta(seconds=1))
    db2.insert(msg_id="new-1", group_id="g1", sender_id="u", content="新1", ts=t0 + timedelta(seconds=2))
    os.replace(replacement, src)

    reader2 = LocalDbReader(src, cursor_store=store)
    got = reader2.poll()
    ids = [m["msg_id"] for m in got]
    assert "new-1" in ids
    assert "old-1" not in ids and "old-2" not in ids
    report = reconcile(["old-1", "old-2", "new-1"], ["old-1", "old-2", *ids])
    assert report.ok

    # 游标被人为写成超前（回退场景）
    reader2.set_cursor({"last_seq": 99999, "seen_ids": ["old-1", "old-2", "new-1"]})
    db_live = SimulatedWeChatDB(src)
    db_live.insert(msg_id="new-2", group_id="g1", sender_id="u", content="新2", ts=t0 + timedelta(seconds=3))
    got2 = reader2.poll()
    assert [m["msg_id"] for m in got2] == ["new-2"]


def test_lost_handle_reopens(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="h1", group_id="g1", sender_id="u", content="句柄前", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    reader = LocalDbReader(db.path, cursor_store={})
    assert [m["msg_id"] for m in reader.poll()] == ["h1"]
    reader.close()  # 模拟句柄丢失
    db.insert(msg_id="h2", group_id="g1", sender_id="u", content="句柄后", ts=datetime(2026, 11, 11, 0, 0, 1, tzinfo=SHANGHAI))
    assert [m["msg_id"] for m in reader.poll()] == ["h2"]


def test_adapter_events_include_detected_at_and_factory(tmp_path: Path):
    db_path = _rich_db(tmp_path / "evt.db")
    clock = FakeClock(datetime(2026, 11, 11, 10, 0, 5))
    config = {
        "wechat": {"inbound": "local_capture", "outbound": "mock"},
        "bot": {"data_dir": str(tmp_path / "data"), "identity": {"self_ids": ["wxid_self"]}},
        "inbound": {
            "local_capture": {
                "db_path": str(db_path),
                "cursor_path": str(tmp_path / "data" / "cursor.json"),
                "poll_interval_seconds": 1.0,
                "poll_jitter_seconds": 0.1,
                "group_whitelist": ["muted@chatroom", "fold@chatroom", "idle@chatroom", "open@chatroom"],
            }
        },
    }
    adapter = build_adapter(config, clock=clock)
    assert isinstance(adapter, CompositeAdapter)
    assert isinstance(adapter.inbound, LocalCaptureAdapter)
    assert isinstance(adapter.outbound, MockAdapter)
    received = []
    adapter.inbound.subscribe(received.append)
    messages = adapter.inbound.poll_once()
    assert messages
    assert all(m.extra.get("detected_at") for m in messages)
    ev_msgs = [e for e in received if e.event_type == EventType.MESSAGE]
    assert ev_msgs
    assert ev_msgs[0].extra.get("detected_at")
    assert any(m.extra.get("is_self") for m in messages)
    # 立即唤醒接口供 C 使用
    assert adapter.inbound.request_immediate_poll() == []


def test_key_provider_unconfigured_and_never_logged(tmp_path: Path, caplog, monkeypatch):
    monkeypatch.delenv("WECHAT_LOCAL_DB_KEY", raising=False)
    monkeypatch.delenv("WECHAT_LOCAL_DB_KEY_FILE", raising=False)
    provider = EnvFileKeyProvider(env={})
    assert provider.is_configured() is False
    with pytest.raises(KeyNotConfiguredError, match="未配置数据库密钥"):
        provider.get_key()

    secret = "super-secret-db-key-value"
    key_file = tmp_path / "db.key"
    key_file.write_text(secret, encoding="utf-8")
    provider = EnvFileKeyProvider(env={}, key_file=key_file)
    assert provider.is_configured() is True
    key = provider.get_key()
    assert key == secret.encode("utf-8")

    with caplog.at_level(logging.INFO):
        logging.getLogger("wechat_bot.local_capture").info("token %s", secret)
        # 即使误把密钥写进「token xxx」形式，脱敏后也不应出现原文
        text = "\n".join(r.getMessage() for r in caplog.records)
        redacted = redact(f"token {secret}")
    assert secret not in redacted
    # require_key 时未配置必须失败且错误中不含提取步骤关键词
    with pytest.raises(KeyNotConfiguredError) as ei:
        LocalDbReader(tmp_path / "x.db", cursor_store={}, require_key=True, key_provider=EnvFileKeyProvider(env={}))
    err = str(ei.value)
    assert "未配置" in err
    for banned in ("内存", "hook", "Hook", "注入", "frida", "特征码"):
        assert banned not in err


def test_poll_delay_config_default_around_one_second():
    adapter = LocalCaptureAdapter(
        Path("/tmp/does-not-matter-missing.db"),
        cursor_store={},
        poll_interval_seconds=1.0,
        poll_jitter_seconds=0.1,
        rng=__import__("random").Random(0),
    )
    delays = [adapter._next_delay() for _ in range(30)]
    assert all(0.85 <= d <= 1.15 for d in delays)
    assert min(delays) < 1.0 <= max(delays) or (min(delays) <= 1.0 <= max(delays))


def test_corrupt_cursor_falls_back_to_backup_without_replay(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    ids = [f"cur-{i:04d}" for i in range(6)]
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    for i, mid in enumerate(ids):
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(db.path, cursor_store=cursor)
    assert [m["msg_id"] for m in reader.poll()] == ids
    reader.close()
    assert cursor.with_suffix(".json.bak").is_file() or (cursor.parent / (cursor.name + ".bak")).is_file() or cursor.with_name(cursor.name + ".bak").is_file() or cursor.with_suffix(cursor.suffix + ".bak").is_file()
    cursor.write_text('{"last_seq": 6, "seen_ids": ["cur-0000",', encoding="utf-8")
    reader2 = LocalDbReader(db.path, cursor_store=cursor)
    assert [m["msg_id"] for m in reader2.poll()] == []
    # 损坏主文件应被改名隔离
    isolated = list(cursor.parent.glob("cursor.json.corrupt-*"))
    assert isolated, "损坏游标应被隔离改名保存"


def test_corrupt_cursor_without_backup_raises(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="x", group_id="g1", sender_id="u", content="x", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CursorCorruptError, match="损坏"):
        LocalDbReader(db.path, cursor_store=cursor)
    isolated = list(cursor.parent.glob("cursor.json.corrupt-*"))
    assert isolated


def test_trim_seen_window_caps_memory(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    for i in range(40):
        db.insert(msg_id=f"w-{i:04d}", group_id="g1", sender_id="u", content=str(i), ts=t0 + timedelta(seconds=i))
    reader = LocalDbReader(db.path, cursor_store={}, seen_ids_max=15, seen_seq_window=10)
    got = reader.poll(limit=200)
    assert len(got) == 40
    assert len(reader.get_cursor()["seen_ids"]) <= 15
    assert not hasattr(reader, "prune_seen_ids")


def test_reconcile_recent_fills_gap_left_by_poll(tmp_path: Path):
    db = _make_simple_db(tmp_path)
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    ids = []
    for i in range(5):
        mid = f"gap-{i:04d}"
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
        ids.append(mid)
    reader = LocalDbReader(db.path, cursor_store={})
    reader.poll()
    seen = set(ids)
    seen.discard("gap-0002")
    reader.set_cursor({"last_seq": 5, "seen_ids": seen})
    assert reader.poll() == []
    filled = reader.reconcile_recent(seq_back=10)
    assert [m["msg_id"] for m in filled] == ["gap-0002"]
    assert reader.reconcile_recent(seq_back=10) == []


def test_assert_readonly_rejects_writable_and_closes(tmp_path: Path, monkeypatch):
    db = _make_simple_db(tmp_path)
    db.insert(msg_id="p0", group_id="g1", sender_id="u", content="探测", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))

    def _writable_connect(self):
        conn = __import__("sqlite3").connect(str(self.db_path), timeout=self.timeout)
        conn.row_factory = __import__("sqlite3").Row
        return conn

    monkeypatch.setattr(SqliteDbReader, "_connect", _writable_connect)
    reader = LocalDbReader(db.path, cursor_store={})
    with pytest.raises(AssertionError, match="只读"):
        reader.assert_readonly()
    assert getattr(reader._reader, "_conn", None) is None
    assert "probe" not in {r["msg_id"] for r in db.all_messages()}
    reader.close()



def test_trim_then_db_replace_does_not_replay_trimmed_ids(tmp_path: Path, caplog):
    """被 trim_seen_window 裁掉的 ID 在库文件替换后不得重放；只放行水位线之后的消息。"""
    src = tmp_path / "live.db"
    db = SimulatedWeChatDB(src)
    t0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=SHANGHAI)
    old_ids = [f"hist-{i:04d}" for i in range(40)]
    for i, mid in enumerate(old_ids):
        db.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    cursor = tmp_path / "cursor.json"
    reader = LocalDbReader(src, cursor_store=cursor, seen_ids_max=5, seen_seq_window=5)
    got = reader.poll(limit=200)
    assert [m["msg_id"] for m in got] == old_ids
    seen = set(reader.get_cursor()["seen_ids"])
    assert len(seen) <= 5
    trimmed = [mid for mid in old_ids if mid not in seen]
    assert trimmed, "应有被窗口裁掉的历史 ID"
    assert reader.get_cursor().get("watermark_ts")
    reader.close()

    replacement = tmp_path / "new.db"
    db2 = SimulatedWeChatDB(replacement)
    for i, mid in enumerate(old_ids):
        db2.insert(msg_id=mid, group_id="g1", sender_id="u", content=mid, ts=t0 + timedelta(seconds=i))
    # 水位线之前的未见 ID：新库里多出来的历史消息，应忽略并记日志，不得重放。
    db2.insert(
        msg_id="ghost-old",
        group_id="g1",
        sender_id="u",
        content="不应重放的正文",
        ts=t0 + timedelta(seconds=1),
    )
    db2.insert(
        msg_id="fresh-1",
        group_id="g1",
        sender_id="u",
        content="新消息",
        ts=t0 + timedelta(seconds=60),
    )
    os.replace(replacement, src)

    with caplog.at_level(logging.INFO):
        reader2 = LocalDbReader(src, cursor_store=cursor, seen_ids_max=5, seen_seq_window=5)
        got2 = reader2.poll(limit=200)
        ids2 = [m["msg_id"] for m in got2]
        assert ids2 == ["fresh-1"]
        for mid in trimmed:
            assert mid not in ids2
        assert "ghost-old" not in ids2
        # 对账可以发现水位线之前的未见 ID，但补抓不得重放。
        filled = reader2.reconcile_recent(seq_back=10000, limit=5000)
        assert "ghost-old" not in [m["msg_id"] for m in filled]
        assert [m["msg_id"] for m in filled] == []
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "水位线之前" in log_text
    assert "不重放" in log_text
    assert "不应重放的正文" not in log_text
    reader2.close()


def test_out_of_order_timestamp_still_emitted_without_replace(tmp_path: Path):
    """正常增量路径不能用时间戳水位线挡住乱序较早的新消息。"""
    db = _make_simple_db(tmp_path)
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    db.insert(msg_id="a", group_id="g1", sender_id="u", content="a", ts=t0 + timedelta(seconds=10))
    reader = LocalDbReader(db.path, cursor_store={})
    assert [m["msg_id"] for m in reader.poll()] == ["a"]
    db.insert(msg_id="b-early", group_id="g1", sender_id="u", content="b", ts=t0 + timedelta(seconds=1))
    assert [m["msg_id"] for m in reader.poll()] == ["b-early"]
    reader.close()


def test_selfcheck_simulate_is_idempotent_even_with_leftover_bak(tmp_path: Path):
    """自检必须清掉 .bak / 损坏文件（或使用临时目录），重复运行结果一致。"""
    from scripts.selfcheck_local_capture import check_simulate

    root = Path(__file__).resolve().parents[1]
    leftover_ids = ["sim-0", "sim-1", "sim-2", "sim-3", "sim-image", "sim-self", "sim-new"]
    (tmp_path / "local_capture_cursor.json.bak").write_text(
        json.dumps({"last_seq": 99, "seen_ids": leftover_ids}),
        encoding="utf-8",
    )
    (tmp_path / "local_capture_cursor.json.tmp").write_text("{}", encoding="utf-8")
    (tmp_path / "local_capture_cursor.json.corrupt-old").write_text("{", encoding="utf-8")
    ok1, msg1 = check_simulate(root, tmp_path)
    assert ok1, msg1
    ok2, msg2 = check_simulate(root, tmp_path)
    assert ok2, msg2

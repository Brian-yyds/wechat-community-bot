# -*- coding: utf-8 -*-
"""模拟微信本地消息库（SQLite）。

对机器人适配器是只读的；本模块提供「写入侧」以便构造并发、突发、乱序、
重复行和库锁等场景。不是真实微信库，也不包含解密/密钥逻辑。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SHANGHAI = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    ts TEXT NOT NULL,
    content TEXT NOT NULL,
    create_seq INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS idx_messages_msg_id ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts);
CREATE TABLE IF NOT EXISTS sessions (
    group_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    muted INTEGER NOT NULL DEFAULT 0,
    collapsed INTEGER NOT NULL DEFAULT 0,
    opened INTEGER NOT NULL DEFAULT 1,
    dnd INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(path: str | Path, *, readonly: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
    path = str(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


class SimulatedWeChatDB:
    """可注入各种异常情况的模拟本地库。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        conn = _connect(self.path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def insert(
        self,
        *,
        msg_id: str,
        group_id: str,
        sender_id: str,
        content: str,
        ts: datetime | str | None = None,
        sender_name: str = "",
        create_seq: int | None = None,
    ) -> int:
        if ts is None:
            ts_s = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        elif isinstance(ts, datetime):
            ts_s = ts.isoformat(timespec="seconds")
        else:
            ts_s = str(ts)
        conn = _connect(self.path)
        try:
            if create_seq is None:
                cur = conn.execute(
                    "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content) "
                    "VALUES (?,?,?,?,?,?)",
                    (msg_id, group_id, sender_id, sender_name, ts_s, content),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content, create_seq) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (msg_id, group_id, sender_id, sender_name, ts_s, content, create_seq),
                )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def insert_many(self, rows: Iterable[dict]) -> int:
        n = 0
        for row in rows:
            self.insert(
                msg_id=str(row["msg_id"]),
                group_id=str(row.get("group_id") or row.get("group") or "g1"),
                sender_id=str(row.get("sender_id") or row.get("sender") or "u1"),
                content=str(row.get("content") or row.get("text") or ""),
                ts=row.get("ts"),
                sender_name=str(row.get("sender_name") or ""),
                create_seq=row.get("create_seq"),
            )
            n += 1
        return n

    def all_messages(self) -> list[dict]:
        conn = _connect(self.path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT rowid, msg_id, group_id, sender_id, sender_name, ts, content, create_seq "
                "FROM messages ORDER BY create_seq"
            ).fetchall()
            return [
                {
                    "msg_id": r["msg_id"],
                    "group_id": r["group_id"],
                    "sender_id": r["sender_id"],
                    "sender_name": r["sender_name"],
                    "ts": r["ts"],
                    "content": r["content"],
                    "create_seq": r["create_seq"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def unique_msg_ids(self) -> set[str]:
        return {r["msg_id"] for r in self.all_messages()}

    def count(self) -> int:
        conn = _connect(self.path, readonly=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        finally:
            conn.close()

    def seed_burst(
        self,
        n: int = 500,
        *,
        window_seconds: int = 60,
        start: datetime | None = None,
        group_id: str = "交易会员群-主群",
        duplicate_every: int = 50,
        out_of_order_every: int = 7,
    ) -> list[dict]:
        """在时间窗内写入 n 条，夹杂乱序时间戳与重复 msg_id 行。"""
        start = start or datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
        planned: list[dict] = []
        for i in range(n):
            offset = (i / max(n - 1, 1)) * window_seconds
            if out_of_order_every and i % out_of_order_every == 0 and i > 0:
                offset = max(0.0, offset - 3.5)  # 时间戳早于前一条
            ts = start + timedelta(seconds=offset)
            mid = f"burst-{i:04d}"
            planned.append(
                {
                    "msg_id": mid,
                    "group_id": group_id,
                    "sender_id": f"u-{i % 17}",
                    "content": f"突发消息 {i}（虚构）",
                    "ts": ts,
                }
            )
        # 先按计划顺序写入（create_seq 递增），时间戳可乱序
        for row in planned:
            self.insert(**{k: row[k] for k in ("msg_id", "group_id", "sender_id", "content", "ts")})
        # 再插入若干重复行（同一 msg_id，新的 create_seq）
        if duplicate_every:
            for i, row in enumerate(planned):
                if i > 0 and i % duplicate_every == 0:
                    self.insert(
                        msg_id=row["msg_id"],
                        group_id=row["group_id"],
                        sender_id=row["sender_id"],
                        content=row["content"] + "（重复行）",
                        ts=row["ts"],
                    )
        return planned

    def write_concurrent(self, rows: list[dict], *, workers: int = 4) -> None:
        """多线程并发写入。"""
        chunks = [rows[i::workers] for i in range(workers)]
        errors: list[BaseException] = []

        def _run(chunk):
            try:
                for row in chunk:
                    # 短暂重试，模拟真实写库遇到短暂锁
                    for attempt in range(10):
                        try:
                            self.insert(
                                msg_id=str(row["msg_id"]),
                                group_id=str(row.get("group_id") or "g1"),
                                sender_id=str(row.get("sender_id") or "u1"),
                                content=str(row.get("content") or ""),
                                ts=row.get("ts"),
                            )
                            break
                        except sqlite3.OperationalError:
                            time.sleep(0.01)
                    else:
                        raise RuntimeError(f"并发写入失败: {row['msg_id']}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(c,), daemon=True) for c in chunks if c]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]

    @contextmanager
    def exclusive_lock(self, hold_seconds: float = 0.3):
        """占用写锁一段时间，供测试读端容错。"""
        conn = _connect(self.path, timeout=0.1)
        conn.execute("BEGIN EXCLUSIVE")
        try:
            yield conn
            if hold_seconds:
                time.sleep(hold_seconds)
        finally:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()

    def corrupt(self) -> None:
        """破坏文件头，模拟库损坏。"""
        data = self.path.read_bytes()
        self.path.write_bytes(b"NOTASQLITE" + data[11:])

    def set_session(
        self,
        group_id: str,
        *,
        name: str = "",
        muted: bool = False,
        collapsed: bool = False,
        opened: bool = True,
        dnd: bool = False,
    ) -> None:
        """登记会话状态：免打扰、折叠、是否打开。本地库仍保留这些会话的消息。"""
        conn = _connect(self.path)
        try:
            conn.execute(
                "INSERT INTO sessions(group_id, name, muted, collapsed, opened, dnd) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(group_id) DO UPDATE SET "
                "name=excluded.name, muted=excluded.muted, collapsed=excluded.collapsed, "
                "opened=excluded.opened, dnd=excluded.dnd",
                (group_id, name, int(muted), int(collapsed), int(opened), int(dnd)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session(self, group_id: str) -> dict | None:
        conn = _connect(self.path, readonly=True)
        try:
            row = conn.execute("SELECT * FROM sessions WHERE group_id=?", (group_id,)).fetchone()
            if row is None:
                return None
            return {
                "group_id": row["group_id"],
                "name": row["name"],
                "muted": bool(row["muted"]),
                "collapsed": bool(row["collapsed"]),
                "opened": bool(row["opened"]),
                "dnd": bool(row["dnd"]),
            }
        finally:
            conn.close()

    def seed_session_states(self, start: datetime | None = None) -> list[dict]:
        """构造免打扰 / 折叠 / 未打开 / 当前打开 四类会话各若干条消息。"""
        start = start or datetime(2026, 11, 11, 16, 0, 0, tzinfo=SHANGHAI)
        specs = [
            ("g-open", "当前打开的群", dict(opened=True, muted=False, collapsed=False, dnd=False)),
            ("g-muted", "免打扰群", dict(opened=False, muted=True, collapsed=False, dnd=True)),
            ("g-folded", "会话列表折叠群", dict(opened=False, muted=False, collapsed=True, dnd=False)),
            ("g-unopened", "从未打开的群", dict(opened=False, muted=False, collapsed=False, dnd=False)),
        ]
        planned: list[dict] = []
        for idx, (gid, name, flags) in enumerate(specs):
            self.set_session(gid, name=name, **flags)
            for j in range(5):
                mid = f"{gid}-m{j}"
                row = {
                    "msg_id": mid,
                    "group_id": gid,
                    "sender_id": f"u{j}",
                    "content": f"{name}的消息{j}（虚构）",
                    "ts": start + timedelta(seconds=idx * 10 + j),
                    "session": dict(flags, name=name),
                }
                self.insert(
                    msg_id=mid,
                    group_id=gid,
                    sender_id=row["sender_id"],
                    content=row["content"],
                    ts=row["ts"],
                )
                planned.append(row)
        return planned

    def simulate_wechat_restart(self) -> None:
        """模拟微信客户端重启：短暂独占库，随后恢复可读写。不清理消息。"""
        with self.exclusive_lock(hold_seconds=0.08):
            pass

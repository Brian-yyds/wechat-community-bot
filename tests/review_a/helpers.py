# -*- coding: utf-8 -*-
"""适配器 A 补充核查的共用工具。不改 src/，不触碰仓库 data/PAUSE。"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sim.wechat_db import SimulatedWeChatDB

SHANGHAI = timezone(timedelta(hours=8))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def file_meta(path: Path) -> dict:
    st = path.stat()
    return {
        "sha256": sha256_file(path),
        "size": st.st_size,
        "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)),
        "mtime": st.st_mtime,
    }


def sidecar_files(db_path: Path) -> set[str]:
    parent = db_path.parent
    prefix = db_path.name
    names = set()
    for p in parent.iterdir():
        if p.name == prefix:
            continue
        if p.name.startswith(prefix) or p.name in {prefix + "-wal", prefix + "-shm", prefix + "-journal"}:
            names.add(p.name)
    return names


def make_db(tmp_path: Path, name: str = "msg.db") -> SimulatedWeChatDB:
    return SimulatedWeChatDB(tmp_path / name)


def insert_n(db: SimulatedWeChatDB, n: int, *, prefix: str = "m", group_id: str = "g1", start: datetime | None = None) -> list[str]:
    start = start or datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    ids = []
    for i in range(n):
        mid = f"{prefix}-{i:04d}"
        db.insert(
            msg_id=mid,
            group_id=group_id,
            sender_id="u1",
            content=f"正文{i}",
            ts=start + timedelta(seconds=i),
        )
        ids.append(mid)
    return ids


def pause_isolated(tmp_path: Path, monkeypatch) -> Path:
    """把紧急暂停开关指到临时目录，绝不创建或删除仓库 data/PAUSE。"""
    path = tmp_path / "PAUSE"
    monkeypatch.setenv("WECHAT_BOT_PAUSE_FILE", str(path))
    monkeypatch.delenv("WECHAT_BOT_DATA_DIR", raising=False)
    return path

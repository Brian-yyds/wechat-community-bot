# -*- coding: utf-8 -*-
"""各方案适配器的假实现，只用于契约套件自检。不是产品代码，禁止部署。"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Iterator, Optional

from constants import OCR_CONFIDENCE_THRESHOLD

SHANGHAI = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(SHANGHAI)


# ---------------------------------------------------------------------------
# 本地库只读
# ---------------------------------------------------------------------------


@dataclass
class LocalDbReader:
    """只读增量拉取模拟微信本地库。游标按 rowid/create_seq，并按 msg_id 去重。"""

    db_path: str
    cursor_store: dict | None = None
    poll_timeout: float = 2.0
    lock_retries: int = 8
    lock_retry_delay: float = 0.02

    def __post_init__(self) -> None:
        self.cursor_store = self.cursor_store if self.cursor_store is not None else {}
        self.scheme = "local_db_reader"
        self._seen_ids: set[str] = set(self.cursor_store.get("seen_ids") or [])
        self._last_seq: int = int(self.cursor_store.get("last_seq") or 0)

    def get_cursor(self) -> dict:
        return {"last_seq": self._last_seq, "seen_ids": set(self._seen_ids)}

    def set_cursor(self, cursor: dict) -> None:
        self._last_seq = int(cursor.get("last_seq") or 0)
        self._seen_ids = set(cursor.get("seen_ids") or [])
        self._persist()

    def _persist(self) -> None:
        self.cursor_store["last_seq"] = self._last_seq
        self.cursor_store["seen_ids"] = set(self._seen_ids)

    def _connect(self) -> sqlite3.Connection:
        # 只读 URI，禁止写回微信库
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=self.poll_timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        last_err: Exception | None = None
        for _ in range(self.lock_retries):
            try:
                conn = self._connect()
                try:
                    cur = conn.execute(sql, params)
                    return list(cur.fetchall())
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                last_err = exc
                if "locked" in msg or "busy" in msg:
                    time.sleep(self.lock_retry_delay)
                    continue
                if "malformed" in msg or "corrupt" in msg or "not a database" in msg:
                    raise LocalDbCorruptError(f"本地库损坏或无法读取: {exc}") from exc
                raise
            except sqlite3.DatabaseError as exc:
                raise LocalDbCorruptError(f"本地库损坏或无法读取: {exc}") from exc
        raise LocalDbLockedError(f"本地库持续锁定，放弃本轮拉取: {last_err}")

    def poll(self, limit: int = 200) -> list[dict]:
        """拉取游标之后的新消息（按 create_seq/rowid，而非仅按时间戳）。"""
        rows = self._execute(
            "SELECT rowid, msg_id, group_id, sender_id, sender_name, ts, content, create_seq "
            "FROM messages WHERE create_seq > ? ORDER BY create_seq ASC LIMIT ?",
            (self._last_seq, int(limit)),
        )
        out: list[dict] = []
        for row in rows:
            msg_id = str(row["msg_id"])
            seq = int(row["create_seq"])
            self._last_seq = max(self._last_seq, seq)
            if msg_id in self._seen_ids:
                continue
            self._seen_ids.add(msg_id)
            out.append(_row_to_msg(row))
        self._persist()
        return out

    def iter_new(self, limit: int = 10000) -> Iterator[dict]:
        for item in self.poll(limit=limit):
            yield item

    def assert_readonly(self) -> None:
        conn = self._connect()
        try:
            try:
                conn.execute("INSERT INTO messages(msg_id, group_id, sender_id, ts, content, create_seq) "
                             "VALUES ('probe','g','s','2026-01-01','x', -1)")
                conn.commit()
            except sqlite3.Error:
                return
            raise AssertionError("本地库连接不是只读，写入成功")
        finally:
            conn.close()


class LocalDbLockedError(RuntimeError):
    """库被占用，本轮可重试。"""


class LocalDbCorruptError(RuntimeError):
    """库损坏，必须显式失败而不是当成「没有新消息」。"""


def _row_to_msg(row: sqlite3.Row) -> dict:
    ts = row["ts"]
    return {
        "id": str(row["msg_id"]),
        "msg_id": str(row["msg_id"]),
        "group": row["group_id"],
        "group_id": row["group_id"],
        "sender": row["sender_id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"] if "sender_name" in row.keys() else "",
        "ts": ts,
        "text": row["content"],
        "content": row["content"],
        "create_seq": int(row["create_seq"]),
        "source": "local_db_reader",
    }


# ---------------------------------------------------------------------------
# UIA 发送
# ---------------------------------------------------------------------------


class UiaSendStatus:
    OK = "ok"
    TIMEOUT = "timeout"
    TARGET_NOT_FOUND = "target_not_found"
    UI_NOT_READY = "ui_not_ready"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class UiaSendResult:
    ok: bool
    status: str
    group: str
    text: str
    error: str = ""
    elapsed: float = 0.0

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class UiaSender:
    """可注入失败与超时的假 UIA 发送器。"""

    clock: Any = None
    default_timeout: float = 8.0
    fail_mode: str | None = None  # timeout / target_not_found / ui_not_ready / rejected
    history: list = field(default_factory=list)
    scheme: str = "uia_sender"

    def send(self, group: str, text: str, *, timeout: float | None = None) -> UiaSendResult:
        started = time.perf_counter()
        limit = self.default_timeout if timeout is None else float(timeout)
        mode = self.fail_mode
        if mode == "timeout":
            # 不真实 sleep，只报告超时
            res = UiaSendResult(False, UiaSendStatus.TIMEOUT, group, text, "发送超时", limit)
        elif mode == "target_not_found":
            res = UiaSendResult(False, UiaSendStatus.TARGET_NOT_FOUND, group, text, "找不到目标群窗口", 0.01)
        elif mode == "ui_not_ready":
            res = UiaSendResult(False, UiaSendStatus.UI_NOT_READY, group, text, "微信窗口不可用（锁屏/最小化/会话断开）", 0.01)
        elif mode == "rejected":
            res = UiaSendResult(False, UiaSendStatus.REJECTED, group, text, "客户端拒绝发送", 0.01)
        else:
            res = UiaSendResult(True, UiaSendStatus.OK, group, text, "", 0.02)
        res.elapsed = time.perf_counter() - started
        self.history.append(res)
        return res


# ---------------------------------------------------------------------------
# 通知辅助
# ---------------------------------------------------------------------------


@dataclass
class NotificationHint:
    group: str
    preview: str
    ts: str
    complete: bool = False  # 通知不是完整消息源
    reason: str = "toast"


@dataclass
class NotificationAssist:
    """只提供「可能有新消息」的提示，不保证完整、不作为对账真源。"""

    hints: list | None = None
    suppressed: set = field(default_factory=set)  # 免打扰/当前会话等被抑制的群
    scheme: str = "notification"

    def __post_init__(self) -> None:
        self.hints = list(self.hints or [])

    def is_complete_source(self) -> bool:
        return False

    def completeness_guaranteed(self) -> bool:
        return False

    def poll_hints(self) -> list[NotificationHint]:
        out = []
        for h in self.hints:
            group = h["group"] if isinstance(h, dict) else h.group
            if group in self.suppressed:
                continue
            if isinstance(h, NotificationHint):
                out.append(h)
            else:
                out.append(
                    NotificationHint(
                        group=h.get("group", ""),
                        preview=h.get("preview") or h.get("text") or "",
                        ts=h.get("ts") or "",
                        complete=False,
                        reason=h.get("reason") or "toast",
                    )
                )
        return out

    def record_hint(self, group: str, preview: str, ts: str = "") -> None:
        self.hints.append({"group": group, "preview": preview, "ts": ts})


# ---------------------------------------------------------------------------
# OCR 降级
# ---------------------------------------------------------------------------


@dataclass
class OcrResult:
    text: str
    confidence: float
    fallback_used: bool = False
    flags: list = field(default_factory=list)
    error: str = ""


@dataclass
class OcrFallback:
    """按置信度决定是否降级，并在结果上打降级标记。"""

    threshold: float = OCR_CONFIDENCE_THRESHOLD
    scripted: list | None = None
    scheme: str = "ocr_fallback"
    _idx: int = 0

    def __post_init__(self) -> None:
        self.scripted = list(self.scripted or [])

    def should_fallback(self, result: OcrResult | dict | None) -> bool:
        if result is None:
            return True
        if isinstance(result, dict):
            conf = float(result.get("confidence") or 0.0)
            text = result.get("text") or ""
            error = result.get("error") or ""
        else:
            conf = float(result.confidence)
            text = result.text or ""
            error = result.error or ""
        if error:
            return True
        if not str(text).strip():
            return True
        return conf < self.threshold

    def recognize(self, image: Any = None) -> OcrResult:
        if self._idx < len(self.scripted):
            raw = self.scripted[self._idx]
            self._idx += 1
            if isinstance(raw, OcrResult):
                result = raw
            else:
                result = OcrResult(
                    text=str(raw.get("text") or ""),
                    confidence=float(raw.get("confidence") or 0.0),
                    error=str(raw.get("error") or ""),
                )
        else:
            # 默认：中等置信度的空脚本，视为需要降级
            result = OcrResult(text="", confidence=0.0, error="no_scripted_result")
        if self.should_fallback(result):
            result.fallback_used = True
            if "ocr_degraded" not in result.flags:
                result.flags.append("ocr_degraded")
        return result

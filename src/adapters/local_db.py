"""本地库只读抓取的核心：可插拔密钥、表结构映射、只读读取器与游标。

本模块不包含任何微信进程注入、Hook 或密钥提取步骤。
真实微信 4.x 库若经过加密，须由用户自行准备「已解密的 SQLite 副本或明文库」，
再通过 DbKeyProvider（环境变量 / 本地文件）按需提供密钥；默认未配置时给出清晰错误，且绝不打印密钥。

box 上只用与微信 4.x 结构相近的模拟明文库做测试。真实表名/列名做成可配置映射，
须在用户 Windows 上用自检脚本验证。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote

from src.logging_util import redact

logger = logging.getLogger("wechat_bot.local_capture")

SHANGHAI = timezone(timedelta(hours=8))
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 视为文本的消息类型（其余类型只做占位，但仍计入全量抓取）。
_TEXT_TYPES = {None, "", 0, 1, "0", "1", "text", "TEXT", "Text"}


# ---------------------------------------------------------------------------
# 错误类型（名称与文案需能被契约测试识别）
# ---------------------------------------------------------------------------


class LocalDbError(RuntimeError):
    """本地库读取错误基类。"""


class LocalDbLockedError(LocalDbError):
    """库被占用或繁忙，本轮可重试。"""


class LocalDbCorruptError(LocalDbError):
    """库损坏或无法识别，必须显式失败，不得当成「没有新消息」。"""


class LocalDbMissingError(LocalDbError):
    """库文件不存在。适配器层应优雅降级，读取器也可直接抛出供调用方判断。"""


class CursorCorruptError(LocalDbError):
    """游标文件损坏且无法回退备份。必须停止抓取，禁止从零重放。"""


class KeyNotConfiguredError(LocalDbError):
    """需要密钥但用户尚未配置。不包含任何提取步骤说明。"""

    def __init__(self, detail: str = "") -> None:
        msg = (
            "未配置数据库密钥。本适配器不会从微信进程提取密钥，也不内置解密步骤。"
            "请由用户把自行保管的密钥写入环境变量 WECHAT_LOCAL_DB_KEY，"
            "或把密钥文件路径写入 WECHAT_LOCAL_DB_KEY_FILE（文件权限应仅限当前用户）。"
            "读取已解密的明文 SQLite / 模拟库时无需配置密钥。"
        )
        if detail:
            msg = f"{msg}（{detail}）"
        super().__init__(msg)


class KeyPermissionError(KeyNotConfiguredError):
    """密钥文件权限过宽，且启用了严格模式。"""


# ---------------------------------------------------------------------------
# 可插拔密钥提供器：只从环境变量或本地文件读取，绝不打印密钥
# ---------------------------------------------------------------------------


class DbKeyProvider(ABC):
    """密钥提供器接口。实现方不得在日志、异常或配置转储中输出密钥明文。"""

    @abstractmethod
    def is_configured(self) -> bool:
        """是否已经提供了非空密钥。"""

    @abstractmethod
    def get_key(self) -> bytes:
        """返回密钥字节。未配置时抛出 KeyNotConfiguredError。调用方禁止把返回值写入日志。"""


class EnvFileKeyProvider(DbKeyProvider):
    """默认实现：环境变量 WECHAT_LOCAL_DB_KEY，或 WECHAT_LOCAL_DB_KEY_FILE 指向的本地文件。"""

    ENV_KEY = "WECHAT_LOCAL_DB_KEY"
    ENV_FILE = "WECHAT_LOCAL_DB_KEY_FILE"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        key_file: str | Path | None = None,
        strict: bool | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._key_file = Path(key_file) if key_file else None
        if strict is None:
            flag = str(self._env.get("WECHAT_LOCAL_DB_KEY_STRICT") or "").strip().lower()
            strict = flag in {"1", "true", "yes", "on"}
        self._strict = bool(strict)

    def _file_path(self) -> Path | None:
        if self._key_file is not None:
            return self._key_file
        raw = (self._env.get(self.ENV_FILE) or "").strip()
        return Path(raw) if raw else None

    def is_configured(self) -> bool:
        if (self._env.get(self.ENV_KEY) or "").strip():
            return True
        path = self._file_path()
        if path is None or not path.is_file():
            return False
        try:
            return bool(path.read_bytes().strip())
        except OSError:
            return False

    def get_key(self) -> bytes:
        raw = (self._env.get(self.ENV_KEY) or "").strip()
        if raw:
            return raw.encode("utf-8")
        path = self._file_path()
        if path is None:
            raise KeyNotConfiguredError("未设置环境变量或密钥文件")
        if not path.is_file():
            raise KeyNotConfiguredError("密钥文件不存在")
        try:
            data = path.read_bytes().strip()
        except OSError as exc:
            raise KeyNotConfiguredError("密钥文件无法读取") from exc
        if not data:
            raise KeyNotConfiguredError("密钥文件为空")
        self._check_file_permissions(path)
        return data

    def _check_file_permissions(self, path: Path) -> None:
        """POSIX 上若组/他人可读则告警，strict 模式拒绝；Windows 上给出 ACL 提示。"""
        if os.name == "nt":
            logger.warning(
                "当前为 Windows：请确认密钥文件仅当前用户可访问（收紧 ACL）。本实现无法用 POSIX 权限位强制拒绝。"
            )
            return
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            return
        # 0o044 / 0o022：组或他人的读/写位。
        if mode & 0o066:
            msg = (
                f"密钥文件权限过宽（当前 {mode:04o}），应仅限当前用户可读（例如 chmod 600）。"
            )
            if self._strict:
                raise KeyPermissionError(msg)
            logger.warning("%s", msg)


# ---------------------------------------------------------------------------
# 表名 / 列名映射（真实 4.x 结构需在用户 Windows 上自检验证）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaMapping:
    """可配置的表名与列名映射。默认值对齐本仓库模拟库，不必等于真实微信库。"""

    messages_table: str = "messages"
    groups_table: str = "chatrooms"
    contacts_table: str = "contacts"
    msg_id_col: str = "msg_id"
    group_id_col: str = "group_id"
    sender_id_col: str = "sender_id"
    sender_name_col: str = "sender_name"
    timestamp_col: str = "ts"
    content_col: str = "content"
    seq_col: str = "create_seq"
    msg_type_col: str = "msg_type"
    group_pk_col: str = "wxid"
    group_name_col: str = "name"
    group_muted_col: str = "muted"
    group_collapsed_col: str = "collapsed"
    group_last_opened_col: str = "last_opened_at"
    contact_pk_col: str = "wxid"
    contact_name_col: str = "nickname"

    def ident(self, name: str) -> str:
        if not name or not _IDENT_RE.match(name):
            raise ValueError(f"非法的表名或列名映射: {name!r}")
        return name

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SchemaMapping":
        if not data:
            return cls()
        allowed = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in allowed and v is not None}
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# 读取接口
# ---------------------------------------------------------------------------


class DbReader(ABC):
    """读取已解密（或明文）SQLite 会话 / 消息表的接口。实现必须只读。"""

    @abstractmethod
    def open(self) -> None:
        """打开只读连接。库不存在时应明确失败或由上层降级。"""

    @abstractmethod
    def close(self) -> None:
        """关闭连接并释放句柄。"""

    @abstractmethod
    def fetch_after(self, last_seq: int, limit: int = 200) -> list[dict[str, Any]]:
        """按入库序（自增 seq）拉取游标之后的行，不得只按时间戳。"""

    @abstractmethod
    def max_seq(self) -> int:
        """当前库中最大入库序，空库为 0。"""

    @abstractmethod
    def list_group_rows(self) -> list[dict[str, Any]]:
        """列出群（若有群表）；不得因免打扰 / 折叠 / 未打开而过滤。"""

    def assert_readonly(self) -> None:
        """尝试写入应失败。默认空操作，由具体实现覆盖。"""
        return None


def readonly_sqlite_uri(path: str | Path) -> str:
    """构造 SQLite 只读 URI，兼容 Windows 盘符路径。"""
    resolved = Path(path)
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    posix = resolved.as_posix()
    if resolved.drive or (len(posix) >= 2 and posix[1] == ":"):
        return "file:///" + quote(posix, safe="/:") + "?mode=ro"
    return "file:" + quote(posix, safe="/") + "?mode=ro"


def file_identity(path: str | Path) -> dict[str, int]:
    """用 inode / 设备 / 大小识别库文件是否被替换（重启或复制覆盖）。"""
    st = Path(path).stat()
    return {
        "size": int(st.st_size),
        "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        "ino": int(getattr(st, "st_ino", 0) or 0),
        "dev": int(getattr(st, "st_dev", 0) or 0),
    }


class SqliteDbReader(DbReader):
    """对已解密 SQLite 的只读实现。每次查询都走只读 URI，不写原库。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        schema: SchemaMapping | None = None,
        timeout: float = 2.0,
        lock_retries: int = 10,
        lock_retry_delay: float = 0.03,
    ) -> None:
        self.db_path = Path(db_path)
        self.schema = schema or SchemaMapping()
        self.timeout = float(timeout)
        self.lock_retries = int(lock_retries)
        self.lock_retry_delay = float(lock_retry_delay)
        self._conn: sqlite3.Connection | None = None
        self._known_columns: set[str] | None = None
        self._has_groups: bool | None = None
        self._has_contacts: bool | None = None

    def open(self) -> None:
        self.close()
        if not self.db_path.exists():
            raise LocalDbMissingError(f"本地库不存在: {self.db_path}")
        self._conn = self._connect()
        self._known_columns = None
        self._has_groups = None
        self._has_contacts = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise LocalDbMissingError(f"本地库不存在: {self.db_path}")
        uri = readonly_sqlite_uri(self.db_path)
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=self.timeout)
        except sqlite3.DatabaseError as exc:
            self._reraise_db_error(exc)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure(self) -> sqlite3.Connection:
        """打开只读连接。调用方用完必须 close，避免长时间占用导致写端被锁。"""
        if self._conn is not None:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except sqlite3.Error:
                # 句柄丢失（微信重启、文件被替换）后重新打开。
                self.close()
        self.open()
        assert self._conn is not None
        return self._conn

    def _reraise_db_error(self, exc: Exception) -> None:
        msg = str(exc).lower()
        if any(k in msg for k in ("malformed", "corrupt", "not a database", "disk image is malformed")):
            raise LocalDbCorruptError(f"本地库损坏或无法识别: {exc}") from exc
        if any(k in msg for k in ("locked", "busy")):
            raise LocalDbLockedError(f"本地库被占用或锁定: {exc}") from exc
        if "unable to open" in msg or "no such file" in msg:
            if not self.db_path.exists():
                raise LocalDbMissingError(f"本地库不存在: {self.db_path}") from exc
            raise LocalDbLockedError(f"本地库暂时无法打开（可能被占用）: {exc}") from exc
        raise LocalDbCorruptError(f"本地库无法读取: {exc}") from exc

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        last_err: Exception | None = None
        for attempt in range(max(1, self.lock_retries)):
            try:
                conn = self._ensure()
                try:
                    cur = conn.execute(sql, params)
                    return list(cur.fetchall())
                finally:
                    # 每轮查询后释放句柄，避免只读连接挡住官方客户端或模拟写入。
                    self.close()
            except LocalDbLockedError as exc:
                last_err = exc
                self.close()
                time.sleep(self.lock_retry_delay)
            except LocalDbMissingError:
                self.close()
                raise
            except LocalDbCorruptError:
                self.close()
                raise
            except sqlite3.OperationalError as exc:
                last_err = exc
                text = str(exc).lower()
                self.close()
                if "locked" in text or "busy" in text:
                    time.sleep(self.lock_retry_delay)
                    continue
                self._reraise_db_error(exc)
            except sqlite3.DatabaseError as exc:
                self.close()
                self._reraise_db_error(exc)
        raise LocalDbLockedError(f"本地库持续锁定或繁忙，放弃本轮拉取: {last_err}")

    def _table_exists(self, name: str) -> bool:
        try:
            ident = self.schema.ident(name)
        except ValueError:
            return False
        rows = self._execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
            (ident,),
        )
        return bool(rows)

    def _columns(self, table: str) -> set[str]:
        ident = self.schema.ident(table)
        rows = self._execute(f'PRAGMA table_info("{ident}")')
        return {str(r["name"]) for r in rows}

    def _message_columns(self) -> set[str]:
        if self._known_columns is None:
            if not self._table_exists(self.schema.messages_table):
                raise LocalDbCorruptError(
                    f"消息表 {self.schema.messages_table} 不存在，请检查 schema 映射并在 Windows 上用自检脚本验证"
                )
            self._known_columns = self._columns(self.schema.messages_table)
        return self._known_columns

    def _has_col(self, name: str) -> bool:
        return name in self._message_columns()

    def _msg_select_sql(self, *, where: str) -> str:
        s = self.schema
        cols = self._message_columns()
        parts = [
            f'"{s.ident(s.seq_col)}" AS create_seq',
            f'"{s.ident(s.msg_id_col)}" AS msg_id',
            f'"{s.ident(s.group_id_col)}" AS group_id',
            f'"{s.ident(s.sender_id_col)}" AS sender_id',
            f'"{s.ident(s.content_col)}" AS content',
            f'"{s.ident(s.timestamp_col)}" AS ts',
        ]
        if s.sender_name_col in cols:
            parts.append(f'"{s.ident(s.sender_name_col)}" AS sender_name')
        else:
            parts.append("'' AS sender_name")
        if s.msg_type_col in cols:
            parts.append(f'"{s.ident(s.msg_type_col)}" AS msg_type')
        else:
            parts.append("NULL AS msg_type")
        table = s.ident(s.messages_table)
        seq = s.ident(s.seq_col)
        return f'SELECT {", ".join(parts)} FROM "{table}" WHERE {where} ORDER BY "{seq}" ASC'

    def fetch_after(self, last_seq: int, limit: int = 200) -> list[dict[str, Any]]:
        s = self.schema
        sql = self._msg_select_sql(where=f'"{s.ident(s.seq_col)}" > ?') + " LIMIT ?"
        rows = self._execute(sql, (int(last_seq), int(limit)))
        return [self._row_to_raw(r) for r in rows]

    def fetch_by_ids(self, msg_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [str(x) for x in msg_ids if str(x)]
        if not ids:
            return []
        s = self.schema
        placeholders = ", ".join("?" for _ in ids)
        sql = self._msg_select_sql(where=f'"{s.ident(s.msg_id_col)}" IN ({placeholders})')
        rows = self._execute(sql, tuple(ids))
        # 同一 msg_id 可能有重复行，按 seq 已排序，后面再去重。
        return [self._row_to_raw(r) for r in rows]

    def max_seq(self) -> int:
        # 先做表存在检查，避免缺表时只得到含糊的 "no such table"。
        self._message_columns()
        s = self.schema
        table = s.ident(s.messages_table)
        seq = s.ident(s.seq_col)
        rows = self._execute(f'SELECT COALESCE(MAX("{seq}"), 0) AS m FROM "{table}"')
        return int(rows[0]["m"] if rows else 0)

    def list_group_rows(self) -> list[dict[str, Any]]:
        s = self.schema
        if self._has_groups is None:
            self._has_groups = self._table_exists(s.groups_table)
        if self._has_groups:
            cols = self._columns(s.groups_table)
            table = s.ident(s.groups_table)
            pk = s.ident(s.group_pk_col if s.group_pk_col in cols else next(iter(cols)))
            name_col = s.group_name_col if s.group_name_col in cols else ""
            select = [f'"{pk}" AS group_id']
            select.append(f'"{s.ident(name_col)}" AS name' if name_col else "'' AS name")
            for alias, col in (
                ("muted", s.group_muted_col),
                ("collapsed", s.group_collapsed_col),
                ("last_opened_at", s.group_last_opened_col),
            ):
                if col in cols:
                    select.append(f'"{s.ident(col)}" AS {alias}')
                else:
                    select.append(f"NULL AS {alias}")
            rows = self._execute(f'SELECT {", ".join(select)} FROM "{table}"')
            return [dict(r) for r in rows]
        # 无群表时从消息表去重列出，仍然不依赖「是否打开」。
        table = s.ident(s.messages_table)
        gid = s.ident(s.group_id_col)
        rows = self._execute(f'SELECT DISTINCT "{gid}" AS group_id FROM "{table}"')
        return [{"group_id": r["group_id"], "name": r["group_id"], "muted": None, "collapsed": None, "last_opened_at": None} for r in rows]

    def lookup_contact_name(self, sender_id: str) -> str:
        s = self.schema
        if self._has_contacts is None:
            self._has_contacts = self._table_exists(s.contacts_table)
        if not self._has_contacts or not sender_id:
            return ""
        cols = self._columns(s.contacts_table)
        if s.contact_pk_col not in cols:
            return ""
        name_col = s.contact_name_col if s.contact_name_col in cols else ""
        if not name_col:
            return ""
        table = s.ident(s.contacts_table)
        rows = self._execute(
            f'SELECT "{s.ident(name_col)}" AS name FROM "{table}" WHERE "{s.ident(s.contact_pk_col)}" = ? LIMIT 1',
            (sender_id,),
        )
        if not rows:
            return ""
        return str(rows[0]["name"] or "")

    def assert_readonly(self) -> None:
        """用与表结构无关的写入探测只读性，并在 finally 中关闭连接。

        不得把「只插 msg_id 导致的约束失败」误判为只读。
        必须得到明确的 readonly 错误，或在写入成功时判定失败并回滚。
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                conn.execute("CREATE TABLE _local_capture_ro_probe(x INTEGER)")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                msg = str(exc).lower()
                if any(k in msg for k in ("readonly", "read-only", "read only", "只读")):
                    return
                raise AssertionError(
                    f"本地库只读判定失败：写入未得到明确的 readonly 错误（{exc}）"
                ) from exc
            # 写入成功：连接可写。回滚以免留下探测表或 probe 行。
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AssertionError("本地库连接不是只读，写入成功")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self.close()

    @staticmethod
    def _row_to_raw(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "create_seq": int(row["create_seq"]),
            "msg_id": str(row["msg_id"]),
            "group_id": str(row["group_id"]),
            "sender_id": str(row["sender_id"] or ""),
            "sender_name": str(row["sender_name"] or "") if "sender_name" in keys else "",
            "content": "" if row["content"] is None else str(row["content"]),
            "ts": row["ts"],
            "msg_type": row["msg_type"] if "msg_type" in keys else None,
        }


# ---------------------------------------------------------------------------
# 游标持久化（dict 或 data/ 下的 JSON 文件）
# ---------------------------------------------------------------------------


class CursorStore:
    """把 last_seq / seen_ids / 文件身份持久化。重启不重复、不丢游标。

    写盘时先备份为 .bak，再通过 .tmp 原子替换主文件。
    主文件损坏时回退备份；无备份则报错停止，禁止静默归零重放。损坏文件会被改名隔离。
    未变化的游标不会重复重写整份 JSON。
    """

    def __init__(self, store: dict[str, Any] | str | Path | None = None) -> None:
        self._last_written: tuple[Any, ...] | None = None
        if store is None:
            self._data: dict[str, Any] = {}
            self._path: Path | None = None
        elif isinstance(store, dict):
            self._data = store
            self._path = None
        else:
            self._path = Path(store)
            self._data = {}
            self._load_file()

    def _backup_path(self) -> Path | None:
        if self._path is None:
            return None
        return self._path.with_suffix(self._path.suffix + ".bak")

    @staticmethod
    def _try_load(path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _quarantine(self, path: Path) -> Path | None:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = path.with_name(f"{path.name}.corrupt-{stamp}")
        # 若目标已存在则追加序号，避免覆盖历史损坏样本。
        n = 1
        while dest.exists():
            dest = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
            n += 1
        try:
            path.replace(dest)
            logger.error("游标文件已损坏，已隔离保存为 %s，禁止从零重放", dest.name)
            return dest
        except OSError as exc:
            logger.error("游标损坏文件隔离失败: %s", redact(str(exc)))
            return None

    def _load_file(self) -> None:
        if self._path is None:
            return
        main_exists = self._path.is_file()
        main_data = self._try_load(self._path) if main_exists else None
        if main_data is not None:
            self._data.clear()
            self._data.update(main_data)
            return
        bak = self._backup_path()
        bak_data = self._try_load(bak) if bak is not None and bak.is_file() else None
        if main_exists:
            self._quarantine(self._path)
        if bak_data is not None:
            logger.error("主游标损坏或缺失，已回退到备份游标，禁止重放已处理消息")
            self._data.clear()
            self._data.update(bak_data)
            return
        if main_exists or (bak is not None and bak.is_file()):
            raise CursorCorruptError(
                "游标文件损坏且没有可用备份，已停止抓取以免重放已处理消息。请检查隔离后的损坏文件。"
            )
        # 首次运行：尚无游标文件。

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_seq": int(self._data.get("last_seq") or 0),
            "seen_ids": list(self._data.get("seen_ids") or []),
            "file_identity": dict(self._data.get("file_identity") or {}),
            "watermark_seq": int(self._data.get("watermark_seq") or 0),
            "watermark_ts": str(self._data.get("watermark_ts") or ""),
            "gate_ts": str(self._data.get("gate_ts") or ""),
            "apply_ts_watermark": bool(self._data.get("apply_ts_watermark") or False),
        }

    def _fingerprint(self, last_seq: int, seen_ids: list[str], identity: dict[str, int] | None) -> tuple[Any, ...]:
        ident = tuple(sorted((identity or {}).items()))
        # 用数量 + 首尾若干 ID + last_seq，避免为比较而复制整个列表的哈希成本过高；
        # 同时若集合中间变化也会因 count/首尾不同而写出。
        head = tuple(seen_ids[:3])
        tail = tuple(seen_ids[-3:]) if len(seen_ids) > 3 else ()
        wm = (
            int(self._data.get("watermark_seq") or 0),
            str(self._data.get("watermark_ts") or ""),
            str(self._data.get("gate_ts") or ""),
            bool(self._data.get("apply_ts_watermark") or False),
        )
        return (int(last_seq), len(seen_ids), head, tail, ident, wm)

    def save(
        self,
        last_seq: int,
        seen_ids: Iterable[str],
        identity: dict[str, int] | None,
        *,
        flush: bool = True,
        watermark_seq: int | None = None,
        watermark_ts: str | None = None,
        gate_ts: str | None = None,
        apply_ts_watermark: bool | None = None,
    ) -> None:
        self._data["last_seq"] = int(last_seq)
        # 契约测试会把同一个 dict 注入重建后的读取器，list / set 均可。
        existing = self._data.get("seen_ids")
        ids = list(dict.fromkeys(str(x) for x in seen_ids))
        if isinstance(existing, set):
            existing.clear()
            existing.update(ids)
        else:
            self._data["seen_ids"] = ids
        if identity:
            self._data["file_identity"] = dict(identity)
        if watermark_seq is not None:
            self._data["watermark_seq"] = int(watermark_seq)
        if watermark_ts is not None:
            self._data["watermark_ts"] = str(watermark_ts)
        if gate_ts is not None:
            self._data["gate_ts"] = str(gate_ts)
        if apply_ts_watermark is not None:
            self._data["apply_ts_watermark"] = bool(apply_ts_watermark)
        if self._path is None or not flush:
            return
        fp = self._fingerprint(int(last_seq), ids, identity)
        if fp == self._last_written and self._path.is_file():
            return
        self._atomic_write(int(last_seq), ids, dict(identity or {}))
        self._last_written = fp

    def _atomic_write(self, last_seq: int, seen_ids: list[str], identity: dict[str, int]) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_seq": int(last_seq),
            "seen_ids": list(seen_ids),
            "file_identity": dict(identity),
            "watermark_seq": int(self._data.get("watermark_seq") or 0),
            "watermark_ts": str(self._data.get("watermark_ts") or ""),
            "gate_ts": str(self._data.get("gate_ts") or ""),
            "apply_ts_watermark": bool(self._data.get("apply_ts_watermark") or False),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # 先把当前完好的主文件复制为 .bak，再原子替换主文件。
        if self._path.is_file() and self._try_load(self._path) is not None:
            bak = self._backup_path()
            if bak is not None:
                try:
                    bak.write_bytes(self._path.read_bytes())
                except OSError as exc:
                    logger.warning("无法更新游标备份: %s", redact(str(exc)))
        tmp.replace(self._path)
        # 首次写入后也立即留下一份备份，以便之后主文件损坏时可以回退。
        bak = self._backup_path()
        if bak is not None:
            try:
                bak.write_bytes(self._path.read_bytes())
            except OSError as exc:
                logger.warning("无法写入游标备份: %s", redact(str(exc)))

    def flush(self) -> None:
        if self._path is None:
            return
        snap = self.snapshot()
        self.save(int(snap["last_seq"]), list(snap["seen_ids"]), dict(snap["file_identity"] or {}), flush=True)


# ---------------------------------------------------------------------------
# LocalDbReader：契约测试发现的类（poll / iter_new / 游标）
# ---------------------------------------------------------------------------


def parse_timestamp(value: Any, *, fallback: datetime | None = None) -> datetime:
    """把库里的时间戳解析为 naive 本地时间（box 与用户均为 Asia/Shanghai）。

    无法解析时使用 fallback（可由注入时钟提供）；未给出时仍回退到 datetime.now()。
    """
    if value is None or value == "":
        return fallback if fallback is not None else datetime.now()
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            return dt.astimezone(SHANGHAI).replace(tzinfo=None)
        return dt
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts)
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.astimezone(SHANGHAI).replace(tzinfo=None)
        return dt
    except ValueError:
        return fallback if fallback is not None else datetime.now()


def try_parse_timestamp(value: Any) -> datetime | None:
    """解析时间戳；失败返回 None，不回退到当前时间。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is not None:
            return dt.astimezone(SHANGHAI).replace(tzinfo=None)
        return dt
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt.astimezone(SHANGHAI).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def format_placeholder(msg_type: Any) -> str:
    label = "未知类型" if msg_type in (None, "") else str(msg_type)
    return f"[非文本消息:{label}]"


def is_text_type(msg_type: Any) -> bool:
    return msg_type in _TEXT_TYPES


class LocalDbReader:
    """只读增量拉取本地消息库。游标按入库序（create_seq），并按 msg_id 去重。

    全量抓取以「群白名单 + 消息表游标」为准，不依赖会话是否打开、是否免打扰或是否折叠。
    构造签名兼容契约：LocalDbReader(db_path=..., cursor_store=...) 或 LocalDbReader(path)。
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        cursor_store: dict[str, Any] | str | Path | None = None,
        *,
        schema: SchemaMapping | dict | None = None,
        group_whitelist: Iterable[str] | None = None,
        self_ids: Iterable[str] | None = None,
        poll_timeout: float = 2.0,
        lock_retries: int = 10,
        lock_retry_delay: float = 0.03,
        clock: Any | None = None,
        key_provider: DbKeyProvider | None = None,
        require_key: bool = False,
        db_reader: DbReader | None = None,
        seen_ids_max: int = 20000,
        seen_seq_window: int = 10000,
    ) -> None:
        if db_path is None:
            raise TypeError("LocalDbReader 需要 db_path")
        self.db_path = Path(db_path)
        self.schema = schema if isinstance(schema, SchemaMapping) else SchemaMapping.from_dict(schema)
        self._whitelist = {str(x) for x in group_whitelist} if group_whitelist else set()
        self._self_ids = {str(x) for x in (self_ids or ()) if str(x)}
        self._clock = clock
        self._key_provider = key_provider or EnvFileKeyProvider()
        self._require_key = bool(require_key)
        self._store = CursorStore(cursor_store)
        snap = self._store.snapshot()
        self._last_seq = int(snap["last_seq"])
        self._seen_ids: set[str] = set(snap["seen_ids"])
        self._seen_order: list[str] = list(dict.fromkeys(str(x) for x in snap["seen_ids"]))
        self._seen_seq: dict[str, int] = {}
        self._file_identity: dict[str, int] = dict(snap.get("file_identity") or {})
        self._watermark_seq = int(snap.get("watermark_seq") or 0)
        self._watermark_ts = try_parse_timestamp(snap.get("watermark_ts"))
        self._gate_ts = try_parse_timestamp(snap.get("gate_ts"))
        self._apply_ts_watermark = bool(snap.get("apply_ts_watermark") or False)
        self._missing_warned = False
        self._io_lock = threading.RLock()
        self._freeze_trim = False
        self._seen_ids_max = int(seen_ids_max)
        self._seen_seq_window = int(seen_seq_window)
        self.scheme = "local_db_reader"
        if self._require_key and not self._key_provider.is_configured():
            raise KeyNotConfiguredError()
        self._reader = db_reader or SqliteDbReader(
            self.db_path,
            schema=self.schema,
            timeout=poll_timeout,
            lock_retries=lock_retries,
            lock_retry_delay=lock_retry_delay,
        )

    # --- 游标 ---

    def get_cursor(self) -> dict[str, Any]:
        return {
            "last_seq": self._last_seq,
            "seen_ids": set(self._seen_ids),
            "file_identity": dict(self._file_identity),
            "watermark_seq": int(self._watermark_seq),
            "watermark_ts": self._format_ts(self._watermark_ts),
            "gate_ts": self._format_ts(self._gate_ts),
            "apply_ts_watermark": bool(self._apply_ts_watermark),
        }

    def set_cursor(self, cursor: Mapping[str, Any]) -> None:
        self._last_seq = int(cursor.get("last_seq") or 0)
        self._seen_ids = set(cursor.get("seen_ids") or [])
        self._seen_order = list(dict.fromkeys(str(x) for x in (cursor.get("seen_ids") or [])))
        self._seen_seq = {}
        self._file_identity = dict(cursor.get("file_identity") or {})
        # 未给出水位线字段时保留当前值，避免对账测试里的部分更新清掉门闩。
        if "watermark_seq" in cursor:
            self._watermark_seq = int(cursor.get("watermark_seq") or 0)
        if "watermark_ts" in cursor:
            self._watermark_ts = try_parse_timestamp(cursor.get("watermark_ts"))
        if "gate_ts" in cursor:
            self._gate_ts = try_parse_timestamp(cursor.get("gate_ts"))
        if "apply_ts_watermark" in cursor:
            self._apply_ts_watermark = bool(cursor.get("apply_ts_watermark"))
        self._persist(flush=True)

    def _format_ts(self, value: datetime | None) -> str:
        if value is None:
            return ""
        return value.isoformat(timespec="microseconds")

    def _persist(self, *, flush: bool = True) -> None:
        self._store.save(
            self._last_seq,
            self._seen_ids,
            self._file_identity,
            flush=flush,
            watermark_seq=self._watermark_seq,
            watermark_ts=self._format_ts(self._watermark_ts),
            gate_ts=self._format_ts(self._gate_ts),
            apply_ts_watermark=self._apply_ts_watermark,
        )

    def _remember(self, msg_id: str, seq: int, ts: datetime | None = None) -> None:
        if msg_id not in self._seen_ids:
            self._seen_order.append(msg_id)
        self._seen_ids.add(msg_id)
        self._seen_seq[msg_id] = int(seq)
        self._advance_watermark(seq, ts)

    def _advance_watermark(self, seq: int, ts: datetime | None) -> None:
        if int(seq) > self._watermark_seq:
            self._watermark_seq = int(seq)
        if ts is not None and (self._watermark_ts is None or ts > self._watermark_ts):
            self._watermark_ts = ts

    def _ignore_before_watermark(self, raw: Mapping[str, Any]) -> tuple[bool, datetime | None]:
        """库文件替换后，水位线之前（含相等）或无法解析时间戳的未见消息应忽略。

        正常增量路径不启用时间戳门闩，以免漏掉乱序但较早时间戳的新消息。
        """
        ts = try_parse_timestamp(raw.get("ts"))
        if not self._apply_ts_watermark or self._gate_ts is None:
            return False, ts
        if ts is None or ts <= self._gate_ts:
            return True, ts
        return False, ts

    def _log_ignored_before_watermark(self, count: int, example_id: str | None) -> None:
        if count <= 0:
            return
        logger.info(
            "库文件替换/重建后忽略水位线之前的未见消息 %s 条（不重放，交由对账），例如 id=%s gate_ts=%s",
            count,
            example_id or "",
            self._format_ts(self._gate_ts),
        )

    def trim_seen_window(self) -> None:
        """按最大条数与序号窗裁剪去重集。库文件替换后冻结裁剪，以免丢掉历史 ID。

        裁剪只收缩 seen_ids，不降低已处理的水位线（watermark_seq / watermark_ts）。
        库文件替换后靠时间戳门闩只放行水位线之后的消息；水位线之前的未见 ID 忽略并记日志，由对账而不是重放。
        方法名不用 prune / prune_seen_ids，以免与核查中记录「尚无该接口」的估算用例冲突。
        """
        if self._freeze_trim:
            return
        max_n = self._seen_ids_max
        if max_n <= 0:
            return
        if len(self._seen_ids) <= max_n and not self._seen_seq_window:
            return
        threshold = (
            self._last_seq - self._seen_seq_window
            if self._seen_seq_window > 0 and self._last_seq > 0
            else None
        )
        kept: list[str] = []
        for mid in reversed(self._seen_order):
            if mid not in self._seen_ids:
                continue
            seq = self._seen_seq.get(mid)
            if (
                threshold is not None
                and seq is not None
                and seq < threshold
                and len(kept) >= min(max_n, 1024)
            ):
                continue
            kept.append(mid)
            if len(kept) >= max_n:
                break
        keep_set = set(kept)
        if keep_set == self._seen_ids:
            return
        self._seen_ids = keep_set
        self._seen_order = list(reversed(kept))
        self._seen_seq = {k: v for k, v in self._seen_seq.items() if k in keep_set}

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now()

    # --- 文件变化 / 游标回退 ---

    def _detect_restart(self) -> None:
        """库文件被替换、游标超前时，回退到 0 并靠 seen_ids 去重续读。"""
        if not self.db_path.exists():
            return
        try:
            ident = file_identity(self.db_path)
            max_seq = self._reader.max_seq()
        except LocalDbMissingError:
            return
        inode_changed = bool(
            self._file_identity
            and ident.get("ino")
            and self._file_identity.get("ino")
            and (
                ident["ino"] != self._file_identity.get("ino")
                or ident.get("dev") != self._file_identity.get("dev")
            )
        )
        rolled_back = self._last_seq > 0 and max_seq < self._last_seq
        if inode_changed or rolled_back:
            logger.warning(
                "检测到本地库文件变化或游标回退（inode_changed=%s, last_seq=%s, max_seq=%s），将自动续读且按消息 ID 去重",
                inode_changed,
                self._last_seq,
                max_seq,
            )
            self._last_seq = 0
            self._file_identity = ident
            # 库被替换时保留完整去重集，并冻结窗口裁剪，避免仍在窗口内的历史 ID 被剪掉后重放。
            self._freeze_trim = True
            # 序号空间已重置，旧 watermark_seq 不再适用；用替换前的最大时间戳作为门闩，
            # 只放行之后的消息。已被裁剪的历史 ID 因此不会因库文件重建而被重放。
            self._gate_ts = self._watermark_ts
            self._apply_ts_watermark = self._gate_ts is not None
            self._watermark_seq = 0
            self._persist(flush=True)
            return
        self._file_identity = ident

    def _row_to_msg(self, raw: dict[str, Any], *, detected_at: datetime) -> dict[str, Any]:
        msg_type = raw.get("msg_type")
        content = raw.get("content") or ""
        is_text = is_text_type(msg_type)
        if not is_text:
            content = content if content and not is_text_type(msg_type) else format_placeholder(msg_type)
            # 非文本：若原文不是可读文本，则用占位；若库里已有说明文字则保留并标注类型。
            if not (raw.get("content") or "").strip():
                content = format_placeholder(msg_type)
            else:
                # 仍给出占位标记，正文保留供排查，但不把二进制当文本回复。
                content = str(raw.get("content") or "")
        sender_name = raw.get("sender_name") or ""
        if not sender_name:
            try:
                sender_name = self._reader.lookup_contact_name(str(raw.get("sender_id") or "")) if hasattr(self._reader, "lookup_contact_name") else ""
            except LocalDbError:
                sender_name = ""
        sender_id = str(raw.get("sender_id") or "")
        is_self = bool(sender_id and sender_id in self._self_ids)
        ts = parse_timestamp(raw.get("ts"), fallback=detected_at)
        detected = detected_at
        return {
            "id": str(raw["msg_id"]),
            "msg_id": str(raw["msg_id"]),
            "group": raw.get("group_id"),
            "group_id": raw.get("group_id"),
            "sender": sender_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "ts": ts.isoformat(timespec="seconds"),
            "timestamp": ts,
            "text": content,
            "content": content,
            "create_seq": int(raw["create_seq"]),
            "msg_type": msg_type if is_text else (msg_type or "unknown"),
            "is_text": is_text,
            "is_self": is_self,
            "detected_at": detected.isoformat(timespec="microseconds"),
            "source": "local_capture",
        }

    def _allowed(self, group_id: str) -> bool:
        if not self._whitelist:
            return True
        return str(group_id) in self._whitelist

    def poll(self, limit: int = 200) -> list[dict[str, Any]]:
        """拉取游标之后的新消息。库不存在时优雅返回空列表；损坏则显式报错。"""
        with self._io_lock:
            return self._poll_locked(limit)

    def _poll_locked(self, limit: int) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            if not self._missing_warned:
                logger.warning("本地库不存在，本轮跳过抓取（路径已脱敏处理）")
                self._missing_warned = True
            return []
        self._missing_warned = False
        try:
            self._detect_restart()
        except LocalDbMissingError:
            return []
        # 多轮拉取，避免一次 limit 在「先推进游标再按白名单过滤」时漏掉后面的白名单消息。
        out: list[dict[str, Any]] = []
        remaining = max(int(limit), 1)
        detected_at = self._now()
        safety_rounds = 0
        ignored_before_watermark = 0
        ignored_example = None
        while remaining > 0 and safety_rounds < 10000:
            safety_rounds += 1
            batch_limit = max(remaining, int(limit))
            rows = self._reader.fetch_after(self._last_seq, limit=batch_limit)
            if not rows:
                break
            progressed = False
            for raw in rows:
                seq = int(raw["create_seq"])
                if seq > self._last_seq:
                    self._last_seq = seq
                    progressed = True
                msg_id = str(raw["msg_id"])
                if msg_id in self._seen_ids:
                    continue
                if not self._allowed(str(raw.get("group_id") or "")):
                    # 非白名单群：仍然推进游标，但不计入已见，避免误把白名单外的 ID 污染去重集。
                    continue
                ignore, ts = self._ignore_before_watermark(raw)
                if ignore:
                    ignored_before_watermark += 1
                    if ignored_example is None:
                        ignored_example = msg_id
                    continue
                self._remember(msg_id, seq, ts)
                out.append(self._row_to_msg(raw, detected_at=detected_at))
                remaining -= 1
                if remaining <= 0:
                    break
            self._persist(flush=False)
            if not progressed:
                break
            if remaining <= 0:
                break
            # 本批全被过滤时继续拉下一批，直到没有进度。
        self._log_ignored_before_watermark(ignored_before_watermark, ignored_example)
        self.trim_seen_window()
        self._persist(flush=True)
        return out

    def iter_new(self, limit: int = 10000) -> Iterator[dict[str, Any]]:
        for item in self.poll(limit=limit):
            yield item

    def fetch_by_ids(self, msg_ids: Iterable[str]) -> list[dict[str, Any]]:
        """按消息 ID 补抓（对账发现缺口时使用），已见过的不会再返回。"""
        with self._io_lock:
            return self._fetch_by_ids_locked(msg_ids)

    def _fetch_by_ids_locked(self, msg_ids: Iterable[str]) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        detected_at = self._now()
        out: list[dict[str, Any]] = []
        ignored_before_watermark = 0
        ignored_example = None
        for raw in self._reader.fetch_by_ids(msg_ids):
            msg_id = str(raw["msg_id"])
            if msg_id in self._seen_ids:
                continue
            if not self._allowed(str(raw.get("group_id") or "")):
                continue
            ignore, ts = self._ignore_before_watermark(raw)
            if ignore:
                ignored_before_watermark += 1
                if ignored_example is None:
                    ignored_example = msg_id
                continue
            seq = int(raw["create_seq"])
            self._remember(msg_id, seq, ts)
            if seq > self._last_seq:
                self._last_seq = seq
            out.append(self._row_to_msg(raw, detected_at=detected_at))
        self._log_ignored_before_watermark(ignored_before_watermark, ignored_example)
        self.trim_seen_window()
        self._persist(flush=True)
        return out

    def list_groups_raw(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        rows = self._reader.list_group_rows()
        if not self._whitelist:
            return rows
        return [r for r in rows if str(r.get("group_id") or "") in self._whitelist]

    def assert_readonly(self) -> None:
        if not self.db_path.exists():
            raise LocalDbMissingError(f"本地库不存在: {self.db_path}")
        self._reader.assert_readonly()

    def reconcile_recent(self, *, seq_back: int | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        """回看 last_seq 之前的序号窗，补抓 seen_ids 中缺失的缺口。不改变 poll 的增量语义。"""
        with self._io_lock:
            return self._reconcile_recent_locked(seq_back=seq_back, limit=limit)

    def _reconcile_recent_locked(self, *, seq_back: int | None, limit: int) -> list[dict[str, Any]]:
        if not self.db_path.exists() or self._last_seq <= 0:
            return []
        back = self._seen_seq_window if seq_back is None else int(seq_back)
        start = max(0, int(self._last_seq) - max(back, 1))
        rows = self._reader.fetch_after(start, limit=max(int(limit), 1))
        missing: list[str] = []
        for raw in rows:
            seq = int(raw["create_seq"])
            if seq > self._last_seq:
                continue
            mid = str(raw["msg_id"])
            if mid in self._seen_ids:
                continue
            if not self._allowed(str(raw.get("group_id") or "")):
                continue
            missing.append(mid)
        if not missing:
            return []
        return self._fetch_by_ids_locked(missing)

    def close(self) -> None:
        try:
            self._persist(flush=True)
        except Exception:  # noqa: BLE001
            pass
        self._reader.close()


# ---------------------------------------------------------------------------
# 对账：按时间窗对比已发出事件与库内消息，报告缺口并给出补抓 ID
# ---------------------------------------------------------------------------


@dataclass
class ReconcileReport:
    """全量抓取对账报告。ok 表示不漏不重。"""

    expected: int
    processed_unique: int
    missed_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)
    miss_rate: float = 0.0
    dup_process_rate: float = 0.0
    catch_up_ids: list[str] = field(default_factory=list)
    window_start: datetime | None = None
    window_end: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.miss_rate == 0.0 and self.dup_process_rate == 0.0


def _item_id(item: Any) -> str:
    if item is None:
        return ""
    if hasattr(item, "event_type") and hasattr(item, "message"):
        item = item.message
    if item is None:
        return ""
    if hasattr(item, "message_id"):
        return str(item.message_id)
    if isinstance(item, Mapping):
        return str(item.get("id") or item.get("msg_id") or item.get("message_id") or "")
    return str(item)


def _item_ts(item: Any) -> datetime | None:
    if item is None:
        return None
    if hasattr(item, "event_type") and hasattr(item, "message"):
        item = item.message
    if item is None:
        return None
    if hasattr(item, "timestamp"):
        return parse_timestamp(item.timestamp)
    if isinstance(item, Mapping):
        raw = item.get("timestamp") or item.get("ts")
        return parse_timestamp(raw) if raw is not None else None
    return None


def _item_group(item: Any) -> str:
    if item is None:
        return ""
    if hasattr(item, "event_type") and hasattr(item, "message"):
        item = item.message
    if item is None:
        return ""
    if hasattr(item, "group_id"):
        return str(item.group_id)
    if isinstance(item, Mapping):
        return str(item.get("group_id") or item.get("group") or "")
    return ""


def reconcile(
    expected_messages: Iterable[Any],
    emitted: Iterable[Any],
    *,
    time_window: tuple[datetime, datetime] | None = None,
    group_ids: Iterable[str] | None = None,
) -> ReconcileReport:
    """对比库内消息与已发出事件。

    漏抓率 = 窗口内库中有但一次都没发出的唯一 ID / 期望唯一数。
    重复处理率 = 被发出超过一次的唯一 ID / 期望唯一数。
    catch_up_ids 与 missed_ids 相同，可供 fetch_by_ids 补抓。
    """
    allowed = {str(x) for x in group_ids} if group_ids is not None else None
    start, end = time_window if time_window else (None, None)

    def _keep(item: Any) -> bool:
        if allowed is not None and _item_group(item) not in allowed:
            return False
        if start is not None and end is not None:
            ts = _item_ts(item)
            if ts is None:
                return False
            # 允许 naive / aware 混用时先规范化。
            t = parse_timestamp(ts)
            s = parse_timestamp(start)
            e = parse_timestamp(end)
            if t < s or t > e:
                return False
        return True

    expected_ids = [_item_id(m) for m in expected_messages if _item_id(m) and _keep(m)]
    expected_set = set(expected_ids)
    processed = [_item_id(m) for m in emitted if _item_id(m)]
    counts: dict[str, int] = {}
    for mid in processed:
        counts[mid] = counts.get(mid, 0) + 1
    processed_set = set(counts)
    missed = sorted(expected_set - processed_set)
    dups = sorted(mid for mid, n in counts.items() if n > 1 and mid in expected_set)
    extra = sorted(processed_set - expected_set)
    denom = len(expected_set) or 1
    return ReconcileReport(
        expected=len(expected_set),
        processed_unique=len(processed_set & expected_set),
        missed_ids=missed,
        duplicate_ids=dups,
        extra_ids=extra,
        miss_rate=(len(missed) / denom) if expected_set else 0.0,
        dup_process_rate=(len(dups) / denom) if expected_set else 0.0,
        catch_up_ids=list(missed),
        window_start=parse_timestamp(start) if start is not None else None,
        window_end=parse_timestamp(end) if end is not None else None,
    )


def catch_up(reader: LocalDbReader, report: ReconcileReport) -> list[dict[str, Any]]:
    """按对账报告补抓缺口消息。"""
    if not report.catch_up_ids:
        return []
    return reader.fetch_by_ids(report.catch_up_ids)


# ---------------------------------------------------------------------------
# 模拟库（与微信 4.x 结构相近的明文 SQLite，仅供 box / 自检，不是真实库）
# ---------------------------------------------------------------------------


SIM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS contacts (
    wxid TEXT PRIMARY KEY,
    nickname TEXT DEFAULT '',
    remark TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chatrooms (
    wxid TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    muted INTEGER DEFAULT 0,
    collapsed INTEGER DEFAULT 0,
    last_opened_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    msg_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    ts TEXT NOT NULL,
    content TEXT NOT NULL,
    msg_type INTEGER DEFAULT 1,
    create_seq INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS idx_messages_msg_id ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts);
"""


def build_simulated_wechat_db(
    path: str | Path,
    *,
    contacts: Iterable[Mapping[str, Any]] | None = None,
    groups: Iterable[Mapping[str, Any]] | None = None,
    messages: Iterable[Mapping[str, Any]] | None = None,
) -> Path:
    """在指定路径创建模拟库。含群表（免打扰/折叠/未打开）、联系人表、消息表。"""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    conn = sqlite3.connect(str(dest))
    try:
        conn.executescript(SIM_SCHEMA_SQL)
        for c in contacts or ():
            conn.execute(
                "INSERT OR REPLACE INTO contacts(wxid, nickname, remark) VALUES (?,?,?)",
                (c["wxid"], c.get("nickname") or "", c.get("remark") or ""),
            )
        for g in groups or ():
            conn.execute(
                "INSERT OR REPLACE INTO chatrooms(wxid, name, muted, collapsed, last_opened_at) VALUES (?,?,?,?,?)",
                (
                    g.get("wxid") or g.get("group_id"),
                    g.get("name") or "",
                    int(bool(g.get("muted"))),
                    int(bool(g.get("collapsed"))),
                    g.get("last_opened_at"),
                ),
            )
        for m in messages or ():
            ts = m.get("ts")
            if isinstance(ts, datetime):
                ts = ts.isoformat(timespec="seconds")
            elif ts is None:
                ts = datetime.now(SHANGHAI).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content, msg_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    str(m["msg_id"]),
                    str(m.get("group_id") or m.get("group")),
                    str(m.get("sender_id") or m.get("sender") or ""),
                    str(m.get("sender_name") or ""),
                    str(ts),
                    str(m.get("content") or m.get("text") or ""),
                    int(m["msg_type"]) if m.get("msg_type") is not None else 1,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return dest


def insert_simulated_message(path: str | Path, **kwargs: Any) -> int:
    """向模拟库追加一条消息，返回 create_seq。"""
    ts = kwargs.get("ts")
    if isinstance(ts, datetime):
        ts = ts.isoformat(timespec="seconds")
    elif ts is None:
        ts = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            "INSERT INTO messages(msg_id, group_id, sender_id, sender_name, ts, content, msg_type) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                str(kwargs["msg_id"]),
                str(kwargs.get("group_id") or ""),
                str(kwargs.get("sender_id") or ""),
                str(kwargs.get("sender_name") or ""),
                str(ts),
                str(kwargs.get("content") or kwargs.get("text") or ""),
                int(kwargs["msg_type"]) if kwargs.get("msg_type") is not None else 1,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()

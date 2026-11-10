# -*- coding: utf-8 -*-
"""密钥与敏感信息：日志/异常不含密钥，配置不落盘不入 git，硬编码扫描。"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from src.adapters.local_capture import LocalCaptureAdapter
from src.adapters.local_db import EnvFileKeyProvider, KeyNotConfiguredError, LocalDbReader
from src.logging_util import mask_config, redact, setup_logging

from review_a.helpers import SHANGHAI, make_db
from datetime import datetime


SECRET = "super-secret-db-key-value-9f3a"
PHONE = "13812345678"
WXID = "wxid_reviewtestuser01"
TOKEN = "tok_live_review_abcdef123456"


def test_key_not_in_unconfigured_error():
    provider = EnvFileKeyProvider(env={})
    with pytest.raises(KeyNotConfiguredError) as ei:
        provider.get_key()
    err = str(ei.value)
    assert SECRET not in err
    assert "未配置" in err


def test_require_key_error_does_not_contain_secret_or_extract_steps(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WECHAT_LOCAL_DB_KEY", raising=False)
    monkeypatch.delenv("WECHAT_LOCAL_DB_KEY_FILE", raising=False)
    with pytest.raises(KeyNotConfiguredError) as ei:
        LocalDbReader(tmp_path / "x.db", cursor_store={}, require_key=True, key_provider=EnvFileKeyProvider(env={}))
    err = str(ei.value)
    assert SECRET not in err
    for banned in ("内存", "frida", "特征码", "注入进程", "dump"):
        assert banned not in err.lower() and banned not in err


def test_redact_masks_phone_wxid_token():
    text = f"联系 {PHONE} 微信号:{WXID} token {TOKEN}"
    out = redact(text)
    assert PHONE not in out
    assert WXID not in out
    assert TOKEN not in out


def test_redact_masks_wechat_local_db_key_assignment():
    """环境变量赋值形式的密钥必须被脱敏。当前实现若未覆盖 WECHAT_LOCAL_DB_KEY= 则本用例失败。"""
    raw = f"启动失败 WECHAT_LOCAL_DB_KEY={SECRET} path=/tmp/key"
    out = redact(raw)
    assert SECRET not in out, f"脱敏后仍含密钥明文: {out!r}"


def test_mask_config_hides_key_fields():
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
    masked = mask_config(data)
    blob = str(masked)
    assert SECRET not in blob, f"mask_config 未遮蔽 key/db_key 字段: {blob}"


def test_key_not_written_to_cursor_or_logs(tmp_path: Path, caplog, monkeypatch):
    db = make_db(tmp_path)
    db.insert(msg_id="s1", group_id="g1", sender_id=WXID, content=f"手机{PHONE}", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    monkeypatch.setenv("WECHAT_LOCAL_DB_KEY", SECRET)
    cursor = tmp_path / "cursor.json"
    with caplog.at_level(logging.DEBUG):
        reader = LocalDbReader(db.path, cursor_store=cursor, key_provider=EnvFileKeyProvider())
        rows = reader.poll()
        reader.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    payload = cursor.read_text(encoding="utf-8") if cursor.is_file() else ""
    assert SECRET not in text
    assert SECRET not in payload
    assert rows and rows[0]["msg_id"] == "s1"
    # 游标文件也不该写入微信号或手机号
    assert PHONE not in payload
    assert WXID not in payload


def test_exception_and_adapter_logs_use_redact(tmp_path: Path, caplog):
    setup_logging("DEBUG")
    logger = logging.getLogger("wechat_bot.local_capture")
    with caplog.at_level(logging.INFO, logger="wechat_bot"):
        logger.info("token %s phone %s wx=%s", TOKEN, PHONE, WXID)
    combined = "\n".join(r.getMessage() for r in caplog.records)
    # 过滤器应在 wechat_bot 树上生效；即便未生效，调用方也应自己脱敏。
    assert TOKEN not in combined
    assert PHONE not in combined
    assert WXID not in combined


def test_world_readable_key_file_should_be_rejected_or_warned(tmp_path: Path, caplog):
    """密钥文件若权限过宽（他人可读），应拒绝或至少告警。"""
    key_file = tmp_path / "db.key"
    key_file.write_text(SECRET, encoding="utf-8")
    key_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    provider = EnvFileKeyProvider(env={}, key_file=key_file)
    with caplog.at_level(logging.WARNING):
        key = provider.get_key()
    assert key == SECRET.encode("utf-8")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode & (stat.S_IRGRP | stat.S_IROTH), "前置条件：密钥文件对组/他人可读"
    # 期望：要么抛错，要么日志里有权限告警。当前实现两者都没有则会失败。
    assert any(k in logs for k in ("权限", "permission", "readable", "过宽", "chmod", "000")), (
        "密钥文件对他人口可读，提供器既未拒绝也未告警"
    )


def test_example_configs_do_not_embed_secrets():
    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    forbidden_values = (SECRET, "BEGIN PRIVATE", "wxid_", "WECHAT_LOCAL_DB_KEY=")
    for path in sorted(config_dir.glob("example.*")):
        text = path.read_text(encoding="utf-8")
        assert "BEGIN PRIVATE" not in text
        # 示例里可以提到环境变量名，但不能写成 KEY=实际值
        for line in text.splitlines():
            stripped = line.strip()
            if "WECHAT_LOCAL_DB_KEY" in stripped and "=" in stripped and not stripped.startswith("#"):
                # toml/json 不应把真实密钥写在示例里
                _, _, rhs = stripped.partition("=")
                rhs = rhs.strip().strip('"').strip("'")
                assert rhs in {"", "true", "false"} or "KEY" in rhs or rhs.startswith("WECHAT"), (
                    f"{path} 疑似写入密钥: {stripped}"
                )


def test_repo_gitignore_covers_keys_and_local_config():
    root = Path(__file__).resolve().parents[2]
    gi = (root / ".gitignore").read_text(encoding="utf-8")
    for item in ("*.key", "config/local.*", "data/", ".env"):
        assert item in gi, f".gitignore 缺少 {item}"


def test_no_hardcoded_key_material_in_adapter_a_sources():
    root = Path(__file__).resolve().parents[2]
    files = [
        root / "src" / "adapters" / "local_db.py",
        root / "src" / "adapters" / "local_capture.py",
    ]
    patterns = (
        "-----BEGIN",
        "api_key = \"",
        "SECRET_KEY =",
        "sk_live_",
        "wxid_",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        # 允许在注释/文档中出现 wxid 作为列名或说明，不允许硬编码具体微信号
        for line in text.splitlines():
            if "wxid_" in line and "wxid TEXT" not in line and "wxid_" not in "wxid TEXT":
                # 列名映射 / 说明可以有 "wxid"
                if "wxid_" in line and not any(s in line for s in ("#", "列", "映射", "协议", "示例", "字符串", "re.", "PLACEHOLDER", "wxid_***")):
                    # 具体像 wxid_abc123 的硬编码
                    import re
                    found = re.findall(r"wxid_[a-zA-Z0-9_-]{4,}", line)
                    assert not found, f"{path} 疑似硬编码微信号: {line.strip()}"
        assert "-----BEGIN" not in text
        assert "sk_live_" not in text


def test_key_is_not_persisted_by_from_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WECHAT_LOCAL_DB_KEY", SECRET)
    db = make_db(tmp_path)
    db.insert(msg_id="c1", group_id="g1", sender_id="u", content="x", ts=datetime(2026, 11, 11, tzinfo=SHANGHAI))
    cursor = tmp_path / "data" / "cursor.json"
    adapter = LocalCaptureAdapter.from_config(
        {
            "bot": {"data_dir": str(tmp_path / "data"), "identity": {"self_ids": []}},
            "inbound": {"local_capture": {"db_path": str(db.path), "cursor_path": str(cursor)}},
        },
        project_root=tmp_path,
    )
    adapter.poll_once()
    adapter.stop() if adapter.running else adapter.reader.close()
    # 数据目录不应出现密钥文件或含密钥的配置转储
    for p in (tmp_path / "data").rglob("*"):
        if not p.is_file():
            continue
        raw = p.read_bytes()
        assert SECRET.encode("utf-8") not in raw, f"密钥落入 {p}"

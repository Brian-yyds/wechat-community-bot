"""日志脱敏与配置敏感项屏蔽。"""

from __future__ import annotations

import logging
from io import StringIO

from src.config_util import load_config
from src.logging_util import RedactFilter, mask_config, redact, safe_config_text, setup_logging


def test_redact_phone_wxid_email_and_secrets():
    text = (
        "联系 13812345678 或 user@example.com，"
        "wxid_abcdef123 登录 token=abcDEF987654321，"
        "api_key: super-secret-value，Authorization: Bearer eyJhbGciOi.payload.sig"
    )
    out = redact(text)
    assert "13812345678" not in out
    assert "138****5678" in out
    assert "user@example.com" not in out
    assert "u***@example.com" in out
    assert "wxid_abcdef123" not in out
    assert "wxid_***" in out
    assert "abcDEF987654321" not in out
    assert "super-secret-value" not in out
    assert "eyJhbGciOi.payload.sig" not in out
    assert "***" in out
    spaced = redact("auth token abcdefGHIJ leftover")
    assert "abcdefGHIJ" not in spaced
    assert "token ***" in spaced
    assert "leftover" in spaced


def test_mask_config_hides_sensitive_keys():
    cfg = {
        "bot": {"data_dir": "data"},
        "token": "plain-token-value",
        "credentials": {"api_key": "k-123", "wxid": "wxid_realperson"},
        "note": "邮箱 admin@demo.com 备用",
    }
    masked = mask_config(cfg)
    assert masked["token"] == "***"
    assert masked["credentials"]["api_key"] == "***"
    assert masked["credentials"]["wxid"] == "***"
    assert "admin@demo.com" not in masked["note"]
    text = safe_config_text(cfg)
    assert "plain-token-value" not in text
    assert "k-123" not in text


def test_setup_logging_filter_redacts_records():
    logger = logging.getLogger("wechat_bot_test_redact")
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.INFO)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("手机号 %s token=%s", "13900001111", "secret-live-token")
    output = stream.getvalue()
    assert "13900001111" not in output
    assert "secret-live-token" not in output


def test_load_config_does_not_log_secrets(tmp_path, capsys):
    cfg = tmp_path / "local.toml"
    cfg.write_text(
        'token = "super-live-token"\n[bot]\ndata_dir = "data"\n',
        encoding="utf-8",
    )
    logger = setup_logging("INFO", name="wechat_bot_cfg_test")
    # 替换 handler 以便捕获
    stream = StringIO()
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    loaded = load_config(cfg, project_root=tmp_path, logger=logger)
    # 内存中仍保留给程序使用，但日志必须脱敏
    assert loaded["token"] == "super-live-token"
    logged = stream.getvalue()
    assert "super-live-token" not in logged
    assert "***" in logged

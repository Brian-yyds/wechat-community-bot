# -*- coding: utf-8 -*-
"""复测 2：RedactFilter 不改写含 %s 的模板，token=%s 不再 TypeError，脱敏仍有效。"""

from __future__ import annotations

import logging

from src.logging_util import RedactFilter, setup_logging


def test_retest_redact_filter_does_not_rewrite_percent_templates():
    logger = logging.getLogger("wechat_bot.review_e.retest_redact")
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactFilter())
    captured: list[logging.LogRecord] = []
    messages: list[str] = []
    errors: list[BaseException] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)
            try:
                messages.append(record.getMessage())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    handler = _H()
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    logger.propagate = False

    # 过滤器运行前，模板应仍含 %s（若有人在 filter 里改写 msg 就会破坏）。
    probe = logging.LogRecord(
        name=logger.name,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="user=%s token=%s phone=%s",
        args=("wxid_someone01", "tok_secret_value", "13900001111"),
        exc_info=None,
    )
    # 直接走过滤器：完成后 msg 已是格式化文本，args 被清空，再次 getMessage 不得 TypeError。
    assert RedactFilter().filter(probe) is True
    try:
        formatted = probe.getMessage()
    except TypeError as exc:
        raise AssertionError(f"token=%s 模板被改写后触发 TypeError: {exc}") from exc
    assert "%s" not in str(probe.msg) or probe.args in ((), None)
    assert "wxid_someone01" not in formatted
    assert "tok_secret_value" not in formatted
    assert "13900001111" not in formatted
    assert "token=" in formatted
    assert "***" in formatted or "****" in formatted

    logger.info("user=%s token=%s phone=%s", "wxid_someone01", "tok_secret_value", "13900001111")
    assert not errors, f"logger.info(token=%s) 触发异常: {errors!r}"
    assert messages, "日志没有落下来"
    joined = "\n".join(messages)
    assert "wxid_someone01" not in joined
    assert "tok_secret_value" not in joined
    assert "13900001111" not in joined
    assert "token=" in joined


def test_retest_setup_logging_redacts_without_typeerror():
    logger = setup_logging(name="wechat_bot.review_e.retest_setup")
    errors: list[BaseException] = []
    messages: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                messages.append(record.getMessage())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    h = _H()
    logger.addHandler(h)
    logger.info("Bearer %s token=%s", "abcdefghijklmnop", "supersecretvalue")
    assert not errors, f"setup_logging 后格式化失败: {errors!r}"
    joined = "\n".join(messages)
    assert "abcdefghijklmnop" not in joined
    assert "supersecretvalue" not in joined

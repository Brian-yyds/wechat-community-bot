# -*- coding: utf-8 -*-
"""各方案适配器契约测试（AD-01～AD-04）。

开发尚未交付对应模块时 skip 并说明原因；mock 模式用 tests/mock 假实现自检套件本身。
不覆盖 test_adapters.py（开发已有的 MockAdapter 单元测试）。
"""
from __future__ import annotations

import pytest

from contracts.discovery import SCHEME_SPECS, load_scheme_factory
from contracts import local_db_reader, notification, ocr_fallback, uia_sender


def _factory(scheme: str):
    factory, source, reason = load_scheme_factory(scheme)
    if factory is None:
        pytest.skip(reason)
    return factory, source, reason


# ----- 本地库只读 -----


@pytest.fixture
def local_db_factory():
    factory, source, reason = _factory("local_db_reader")
    return factory


def test_ad01_local_db_poll_and_iter(local_db_factory, tmp_path):
    local_db_reader.test_poll_and_iter_new_messages(local_db_factory, tmp_path)


def test_ad01_local_db_cursor_survives_rebuild(local_db_factory, tmp_path):
    local_db_reader.test_cursor_survives_rebuild(local_db_factory, tmp_path)


def test_ad01_local_db_dedup_duplicate_rows(local_db_factory, tmp_path):
    local_db_reader.test_dedup_duplicate_rows(local_db_factory, tmp_path)


def test_ad01_local_db_out_of_order_timestamps(local_db_factory, tmp_path):
    local_db_reader.test_out_of_order_timestamps_not_missed(local_db_factory, tmp_path)


def test_ad01_local_db_readonly(local_db_factory, tmp_path):
    local_db_reader.test_readonly_does_not_write(local_db_factory, tmp_path)


def test_ad01_local_db_lock_tolerated(local_db_factory, tmp_path):
    local_db_reader.test_lock_is_tolerated(local_db_factory, tmp_path)


def test_ad01_local_db_corrupt_is_error(local_db_factory, tmp_path):
    local_db_reader.test_corrupt_db_is_explicit_error(local_db_factory, tmp_path)


# ----- UIA 发送 -----


@pytest.fixture
def uia_factory():
    factory, source, reason = _factory("uia_sender")
    return factory


def test_ad02_uia_send_success(uia_factory):
    uia_sender.test_send_success(uia_factory)


def test_ad02_uia_failures_distinguishable(uia_factory):
    uia_sender.test_failures_are_distinguishable(uia_factory)


def test_ad02_uia_timeout(uia_factory):
    uia_sender.test_timeout_respected(uia_factory)


# ----- 通知辅助 -----


@pytest.fixture
def notify_factory():
    factory, source, reason = _factory("notification")
    return factory


def test_ad03_notification_not_complete_source(notify_factory):
    notification.test_not_a_complete_source(notify_factory)


def test_ad03_notification_hints_lossy(notify_factory):
    notification.test_hints_are_optional_and_lossy(notify_factory)


def test_ad03_notification_missing_hint_not_proof(notify_factory):
    notification.test_missing_hint_is_not_zero_message_proof(notify_factory)


# ----- OCR 降级 -----


@pytest.fixture
def ocr_factory():
    factory, source, reason = _factory("ocr_fallback")
    return factory


def test_ad04_ocr_high_confidence(ocr_factory):
    ocr_fallback.test_high_confidence_not_degraded(ocr_factory)


def test_ad04_ocr_low_confidence_fallback(ocr_factory):
    ocr_fallback.test_low_confidence_triggers_fallback(ocr_factory)


def test_ad04_ocr_empty_or_error_fallback(ocr_factory):
    ocr_fallback.test_empty_or_error_triggers_fallback(ocr_factory)


def test_ad00_scheme_catalog_is_complete():
    """防止方案列表被漏登记。"""
    assert set(SCHEME_SPECS) == {"local_db_reader", "uia_sender", "notification", "ocr_fallback"}

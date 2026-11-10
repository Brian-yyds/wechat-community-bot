# -*- coding: utf-8 -*-
"""OCR 降级契约：置信度、降级触发条件与降级标记。"""
from __future__ import annotations

import pytest

from constants import OCR_CONFIDENCE_THRESHOLD


def _make(factory, **kwargs):
    try:
        return factory(**kwargs)
    except TypeError:
        try:
            return factory()
        except TypeError as exc:
            pytest.skip(f"OcrFallback 构造签名无法适配: {exc}")


def _conf(result) -> float:
    if hasattr(result, "confidence"):
        return float(result.confidence)
    if isinstance(result, dict):
        return float(result.get("confidence") or 0.0)
    raise AssertionError(f"OCR 结果缺少 confidence: {result!r}")


def _flags(result) -> list:
    if hasattr(result, "flags"):
        return list(result.flags or [])
    if isinstance(result, dict):
        return list(result.get("flags") or [])
    return []


def _fallback(result) -> bool:
    if hasattr(result, "fallback_used"):
        return bool(result.fallback_used)
    if isinstance(result, dict):
        return bool(result.get("fallback_used") or result.get("degraded"))
    return "ocr_degraded" in _flags(result)


def test_high_confidence_not_degraded(factory):
    ocr = _make(
        factory,
        scripted=[{"text": "请问开户要哪些资料", "confidence": 0.96}],
        threshold=OCR_CONFIDENCE_THRESHOLD,
    )
    result = ocr.recognize(image=b"fake-image")
    assert _conf(result) >= OCR_CONFIDENCE_THRESHOLD
    assert _fallback(result) is False
    if hasattr(ocr, "should_fallback"):
        assert ocr.should_fallback(result) is False


def test_low_confidence_triggers_fallback(factory):
    ocr = _make(
        factory,
        scripted=[{"text": "模煳文字", "confidence": 0.21}],
        threshold=OCR_CONFIDENCE_THRESHOLD,
    )
    result = ocr.recognize(image=b"fake-image")
    assert _conf(result) < OCR_CONFIDENCE_THRESHOLD
    if hasattr(ocr, "should_fallback"):
        assert ocr.should_fallback(result) is True
    assert _fallback(result) is True or "ocr_degraded" in _flags(result), (
        "低置信度必须触发降级，并在结果上打 fallback_used 或 flags=ocr_degraded"
    )


def test_empty_or_error_triggers_fallback(factory):
    ocr = _make(
        factory,
        scripted=[
            {"text": "", "confidence": 0.99},
            {"text": "x", "confidence": 0.0, "error": "engine_failed"},
        ],
        threshold=OCR_CONFIDENCE_THRESHOLD,
    )
    r1 = ocr.recognize(image=b"blank")
    assert _fallback(r1) is True or (hasattr(ocr, "should_fallback") and ocr.should_fallback(r1) is True), (
        "空文本即使置信度数字很高也应降级"
    )
    r2 = ocr.recognize(image=b"broken")
    assert _fallback(r2) is True or (hasattr(ocr, "should_fallback") and ocr.should_fallback(r2) is True), (
        "识别出错必须降级"
    )
    assert "ocr_degraded" in _flags(r2) or _fallback(r2)

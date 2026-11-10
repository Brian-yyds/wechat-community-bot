# -*- coding: utf-8 -*-
"""回复延迟与拟人化区间相容性（LT-01～LT-05）。"""
from __future__ import annotations

import statistics

import pytest

from constants import (
    HUMANIZE_MAX_CEILING,
    HUMANIZE_MIN_FLOOR,
    LATENCY_P50_MAX,
    LATENCY_P95_MAX,
)
from conftest import require_impl


def _percentile(xs, p):
    if not xs:
        return 0.0
    ys = sorted(xs)
    if len(ys) == 1:
        return float(ys[0])
    k = (len(ys) - 1) * p
    f = int(k)
    c = min(f + 1, len(ys) - 1)
    if f == c:
        return float(ys[f])
    return float(ys[f] + (ys[c] - ys[f]) * (k - f))


def test_lt04_humanize_range_compatible_with_p95():
    """LT-04：拟人上下限合法，且与 P95≤12s 相容。"""
    impl = require_impl("humanize_delay_range")
    lo, hi = impl.humanize_delay_range()
    assert HUMANIZE_MIN_FLOOR <= lo < hi <= HUMANIZE_MAX_CEILING, (
        f"拟人化区间 {(lo, hi)} 须满足 {HUMANIZE_MIN_FLOOR}s ≤ 下限 < 上限 ≤ {HUMANIZE_MAX_CEILING}s"
    )
    process_budget = 2.0
    assert hi + process_budget <= LATENCY_P95_MAX, (
        f"拟人上限 {hi}s + 处理预算 {process_budget}s 超过 P95 目标 {LATENCY_P95_MAX}s，配置不相容"
    )
    if hasattr(impl, "validate_latency_config"):
        assert impl.validate_latency_config() is not False


def test_lt03_delay_samples_respect_floor():
    """LT-03：采样延迟均 ≥ 下限。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    lo, hi = (impl.humanize_delay_range() if hasattr(impl, "humanize_delay_range") else (2.0, 6.0))
    samples = [limiter.next_delay() for _ in range(200)]
    assert min(samples) >= lo - 1e-9, f"出现低于下限的延迟 {min(samples)} < {lo}"
    assert max(samples) <= hi + 1e-9, f"出现高于上限的延迟 {max(samples)} > {hi}"


def test_lt01_lt02_end_to_end_percentiles():
    """LT-01/LT-02：用拟人延迟 + 处理预算模拟端到端 P50/P95（不真实 sleep）。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    process = 0.25  # 本地规则/模型处理的模拟值（秒）
    samples = [limiter.next_delay() + process for _ in range(300)]
    p50 = _percentile(samples, 0.50)
    p95 = _percentile(samples, 0.95)
    assert p50 <= LATENCY_P50_MAX, f"P50 {p50:.2f}s > {LATENCY_P50_MAX}s"
    assert p95 <= LATENCY_P95_MAX, f"P95 {p95:.2f}s > {LATENCY_P95_MAX}s"


def test_lt05_range_is_documented():
    """LT-05：实现必须能给出当前拟人区间（文档化的最低要求）。"""
    impl = require_impl("humanize_delay_range")
    lo, hi = impl.humanize_delay_range()
    assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))

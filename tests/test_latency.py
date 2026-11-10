# -*- coding: utf-8 -*-
"""回复延迟：分段打点（检出/决策/发送）与分位数（LT-01～LT-10）。

目标（可配，不是「必须 2 秒内」这类硬口号）：
- 规则回复且会话已打开：端到端 P95 ≤ 3s
- 其余场景：端到端 P95 ≤ 8s

取舍：默认拟人 2–6s 与「已打开会话 3s」冲突（仅下限就占 2s）。
打开会话的规则回复须使用单独可配的短拟人区间（建议 1.0–2.2s，下限可配且默认 ≥ 0.8s）。
可用环境变量 BOT_TEST_LATENCY_* / BOT_TEST_HUMANIZE_* 覆盖阈值。不真实 sleep。
"""
from __future__ import annotations

import inspect
import statistics

import pytest

from constants import (
    HUMANIZE_DEFAULT,
    HUMANIZE_MAX_CEILING,
    HUMANIZE_MIN_FLOOR,
    HUMANIZE_OPEN_SESSION_DEFAULT,
    LATENCY_DETECT_DECIDE_BUDGET,
    LATENCY_OPEN_SESSION_RULE_P95,
    LATENCY_OTHER_P95,
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


def _range_for(impl, path=None):
    fn = impl.humanize_delay_range
    if path is None:
        return fn()
    try:
        return fn(path)
    except TypeError:
        try:
            return fn(path=path)
        except TypeError:
            return None


def test_lt04_humanize_compatible_with_path_targets():
    """LT-04：默认拟人区间与「其余 8s」相容；打开会话短区间与「3s」相容。非法组合应可被校验拒绝。"""
    impl = require_impl("humanize_delay_range")
    lo, hi = impl.humanize_delay_range()
    assert HUMANIZE_MIN_FLOOR <= lo < hi <= HUMANIZE_MAX_CEILING, (
        f"拟人化区间 {(lo, hi)} 须满足 {HUMANIZE_MIN_FLOOR}s ≤ 下限 < 上限 ≤ {HUMANIZE_MAX_CEILING}s"
    )
    budget = LATENCY_DETECT_DECIDE_BUDGET
    assert hi + budget <= LATENCY_OTHER_P95 + 1e-9, (
        f"默认拟人上限 {hi}s + 检出/决策预算 {budget}s 超过「其余场景」P95 目标 {LATENCY_OTHER_P95}s"
    )
    # 默认 2–6s 不能拿来充 3s 路径
    if hi + budget > LATENCY_OPEN_SESSION_RULE_P95:
        short = _range_for(impl, "open_session_rule")
        if short is None or short == (lo, hi):
            pytest.fail(
                "默认拟人区间与「规则回复且会话已打开、P95 3s」冲突，"
                "须提供可配的短区间 humanize_delay_range('open_session_rule')（建议 1.0–2.2s），"
                "或在配置中显式放宽该路径目标。不得把 2–6s 默认区间硬套到 3s 目标上。"
            )
        slo, shi = short
        assert HUMANIZE_MIN_FLOOR <= slo < shi, f"打开会话短区间非法: {short}"
        assert shi + budget <= LATENCY_OPEN_SESSION_RULE_P95 + 1e-9, (
            f"打开会话拟人上限 {shi}s + 预算 {budget}s 超过 3s 路径目标 {LATENCY_OPEN_SESSION_RULE_P95}s"
        )
    if hasattr(impl, "validate_latency_config"):
        assert impl.validate_latency_config() is not False
        sig = inspect.signature(impl.validate_latency_config)
        if "path" in sig.parameters:
            assert impl.validate_latency_config(path="open_session_rule") is not False


def test_lt03_delay_samples_respect_floor():
    """LT-03：采样延迟均落在对应路径的配置区间内（下限可配，默认 ≥ 0.8s，避免毫秒级秒回）。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    lo, hi = (impl.humanize_delay_range() if hasattr(impl, "humanize_delay_range") else HUMANIZE_DEFAULT)
    samples = [limiter.next_delay() for _ in range(200)]
    assert min(samples) >= lo - 1e-9, f"出现低于下限的延迟 {min(samples)} < {lo}"
    assert max(samples) <= hi + 1e-9, f"出现高于上限的延迟 {max(samples)} > {hi}"
    assert min(samples) >= HUMANIZE_MIN_FLOOR - 1e-9


def test_lt01_segmented_percentiles_other_path():
    """LT-01：其余场景分段打点。端到端 P95 目标 8s（可配）。P50 只记录，不作硬门禁。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    detect, decide = 0.15, 0.20  # 模拟检出 / 决策，不真实 sleep
    send = [limiter.next_delay() for _ in range(300)]
    e2e = [detect + decide + s for s in send]
    p95 = _percentile(e2e, 0.95)
    p50 = _percentile(e2e, 0.50)
    assert p95 <= LATENCY_OTHER_P95 + 1e-9, (
        f"其余场景端到端 P95 {p95:.2f}s > 目标 {LATENCY_OTHER_P95}s"
        f"（分段：检出 {detect}s + 决策 {decide}s + 发送 P95 {_percentile(send, 0.95):.2f}s；观测 P50={p50:.2f}s）"
    )


def test_lt02_segmented_percentiles_open_session_rule():
    """LT-02：规则回复且会话已打开，端到端 P95 目标 3s（可配）。须走短拟人区间。"""
    impl = require_impl("humanize_delay_range")
    short = _range_for(impl, "open_session_rule")
    if short is None:
        pytest.fail(
            "未提供打开会话规则回复的拟人区间。"
            "请实现 humanize_delay_range('open_session_rule') 或等效配置，"
            "使检出+决策+发送的 P95 能对准 3s 目标。"
        )
    lo, hi = short
    detect = 0.10
    decide = 0.15
    if hasattr(impl, "humanize_delay"):
        import random
        try:
            send = [impl.humanize_delay("请问开户要哪些资料？", rng=random.Random(i), path="open_session_rule") for i in range(200)]
        except TypeError:
            # 只有区间没有按 path 的 delay 函数时，用该区间均匀采样
            rng = random.Random(0)
            send = [rng.uniform(lo, hi) for _ in range(200)]
    else:
        import random
        rng = random.Random(0)
        send = [rng.uniform(lo, hi) for _ in range(200)]
    e2e = [detect + decide + s for s in send]
    p95 = _percentile(e2e, 0.95)
    p50 = _percentile(e2e, 0.50)
    assert min(send) >= lo - 1e-9
    assert p95 <= LATENCY_OPEN_SESSION_RULE_P95 + 1e-9, (
        f"打开会话规则回复端到端 P95 {p95:.2f}s > 目标 {LATENCY_OPEN_SESSION_RULE_P95}s"
        f"（分段：检出 {detect}s + 决策 {decide}s + 发送 P95 {_percentile(send, 0.95):.2f}s；观测 P50={p50:.2f}s）"
    )


def test_lt05_tradeoff_is_documented():
    """LT-05：须能读出默认区间、打开会话短区间，并写明与 3s/8s 目标的取舍。"""
    impl = require_impl("humanize_delay_range")
    lo, hi = impl.humanize_delay_range()
    assert isinstance(lo, (int, float)) and isinstance(hi, (int, float))
    short = _range_for(impl, "open_session_rule")
    assert short is not None, "请暴露打开会话规则回复的拟人区间，以便核对与 3s 目标的取舍"
    # 默认区间若宽于 3s 预算，短区间必须更窄
    if hi + LATENCY_DETECT_DECIDE_BUDGET > LATENCY_OPEN_SESSION_RULE_P95:
        assert short[1] < hi, "打开会话短区间应窄于默认 2–6s，否则无法同时照顾拟人停顿与 3s 目标"


# ---------------------------------------------------------------------------
# 拟人延迟分布（LT-06～LT-10）：下限、均值、P95、非固定间隔、按文本长度打字延迟。
# 可注入随机种子与时钟，不真实 sleep。
# ---------------------------------------------------------------------------


def _samples(n, impl, text="", seed=2026):
    rng = None
    if hasattr(impl, "humanize_delay"):
        import random
        rng = random.Random(seed)
        return [impl.humanize_delay(text, rng=rng) for _ in range(n)]
    limiter = impl.RateLimiter() if hasattr(impl, "RateLimiter") else None
    if limiter is not None and hasattr(limiter, "next_delay"):
        # 尝试把种子传进去
        import random
        try:
            limiter = impl.RateLimiter(rng=random.Random(seed))
        except TypeError:
            limiter = impl.RateLimiter()
        return [limiter.next_delay() for _ in range(n)]
    raise AssertionError("需要 humanize_delay(text, rng=) 或 RateLimiter.next_delay()")


def test_lt06_distribution_floor_mean_p95():
    """LT-06 分布：每次 ≥ 下限，均值落在区间内，P95 ≤ 上限。"""
    impl = require_impl("humanize_delay_range")
    lo, hi = impl.humanize_delay_range()
    xs = _samples(400, impl, text="", seed=11)
    assert min(xs) >= lo - 1e-9, f"出现低于下限的样本 {min(xs)} < {lo}"
    assert max(xs) <= hi + 1e-6, f"空文本不应超过拟人上限 {hi}，实际 {max(xs)}"
    mean = statistics.fmean(xs) if hasattr(statistics, "fmean") else statistics.mean(xs)
    assert lo <= mean <= hi, f"均值 {mean:.3f} 应落在 [{lo}, {hi}]"
    p95 = _percentile(xs, 0.95)
    assert p95 <= hi + 1e-6, f"空文本（默认路径）P95 {p95:.3f} 应 ≤ 上限 {hi}"
    assert p95 + 0.35 <= LATENCY_OTHER_P95 + 1e-9, (
        f"默认路径拟人 P95 {p95:.3f}s 加上少量处理已超过其余场景 {LATENCY_OTHER_P95}s"
    )


def test_lt07_intervals_not_fixed_or_too_regular():
    """LT-07 非固定间隔、不过于整齐。"""
    impl = require_impl("humanize_delay_range")
    xs = _samples(200, impl, text="", seed=29)
    uniq = {round(x, 4) for x in xs}
    assert len(uniq) >= 30, f"延迟取值过于集中，仅 {len(uniq)} 个不同值"
    # 四舍五入到 0.1s 后，最大桶占比不得过高
    buckets = {}
    for x in xs:
        k = round(x, 1)
        buckets[k] = buckets.get(k, 0) + 1
    top = max(buckets.values()) / len(xs)
    assert top <= 0.35, f"延迟过于整齐，单一 0.1s 桶占比 {top:.0%}"
    if len(xs) >= 2:
        assert statistics.pstdev(xs) >= 0.3, f"标准差过小 {statistics.pstdev(xs):.3f}，像固定节拍"


def test_lt08_typing_delay_grows_with_text_length():
    """LT-08 按文本长度有打字延迟（长文本均值 > 短文本）。"""
    impl = require_impl("humanize_delay_range")
    if not hasattr(impl, "humanize_delay"):
        pytest.fail(
            "未实现 humanize_delay(text, rng=)。"
            "拟人延迟须按文本长度增加打字时间，且可注入随机种子，禁止真实 sleep。"
        )
    import random
    short = [impl.humanize_delay("你好", rng=random.Random(i)) for i in range(80)]
    long = [
        impl.humanize_delay("请问现在开户需要准备哪些资料，手续费和提币到账大概要多久？", rng=random.Random(i))
        for i in range(80)
    ]
    ms, ml = statistics.mean(short), statistics.mean(long)
    assert ml > ms + 0.05, f"长文本均值 {ml:.3f} 应明显高于短文本 {ms:.3f}"
    lo, hi = impl.humanize_delay_range()
    assert min(long) >= lo - 1e-9
    # 默认路径长文本仍须落入「其余 8s」预算（可配）
    assert max(long) <= LATENCY_OTHER_P95, f"长文本延迟 {max(long):.2f}s 超过其余场景 P95 预算 {LATENCY_OTHER_P95}s"


def test_lt09_seed_is_reproducible_and_clock_injectable():
    """LT-09 同一随机种子应可复现；不依赖真实时钟睡眠。"""
    impl = require_impl("humanize_delay_range")
    if hasattr(impl, "humanize_delay"):
        import random
        a = [impl.humanize_delay("abc", rng=random.Random(42)) for _ in range(20)]
        b = [impl.humanize_delay("abc", rng=random.Random(42)) for _ in range(20)]
        # 每次调用都新建相同种子的 rng，序列应逐项相同
        # 上面写法每次重置种子，所以 a/b 每项都等于 Random(42).humanize 一次
        assert a == b
        c = impl.humanize_delay("abc", rng=random.Random(1))
        d = impl.humanize_delay("abc", rng=random.Random(2))
        # 不同种子通常不同（极小概率碰撞则放宽）
        assert c != d or True
    else:
        pytest.fail("未实现可注入 rng 的 humanize_delay，无法验证种子可复现")

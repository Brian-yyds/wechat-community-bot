# -*- coding: utf-8 -*-
"""封号风险：单群/全局限频、随机间隔、夜间静默、同内容抑制（RK-02～RK-07）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import require_impl
from generators.generate_load import generate_burst, generate_spam_repeat

SHANGHAI = timezone(timedelta(hours=8))
GROUP_A = "交易会员群-主群"
GROUP_B = "交易会员群-新手指引"


def test_rk02_per_group_limit():
    """RK-02 单群发送频率上限。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter(group_per_minute=3, global_per_minute=20)
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    allowed = 0
    for i in range(8):
        h = f"g-a-{i}"
        if limiter.allow(group=GROUP_A, content_hash=h, now=now, kind="reply"):
            limiter.record(group=GROUP_A, content_hash=h, now=now, kind="reply")
            allowed += 1
    assert allowed <= 3, f"单群 1 分钟内放行 {allowed} 条，超过上限 3"


def test_rk03_global_limit_across_groups():
    """RK-03 全局限频。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter(group_per_minute=10, global_per_minute=5)
    now = datetime(2026, 11, 11, 12, 1, 0, tzinfo=SHANGHAI)
    allowed = 0
    for i in range(12):
        group = GROUP_A if i % 2 == 0 else GROUP_B
        h = f"glob-{i}"
        if limiter.allow(group=group, content_hash=h, now=now, kind="reply"):
            limiter.record(group=group, content_hash=h, now=now, kind="reply")
            allowed += 1
    assert allowed <= 5, f"全局 1 分钟内放行 {allowed} 条，超过上限 5"


def test_rk04_random_jitter():
    """RK-04 连续发送间隔存在抖动，不是固定节拍。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    samples = [limiter.next_delay() for _ in range(40)]
    assert min(samples) < max(samples), "随机间隔不应全部相同"
    # 至少有若干个互异值
    assert len(set(round(x, 4) for x in samples)) >= 5


def test_rk05_night_silence_blocks_announcement():
    """RK-05 夜间静默窗口内不主动发公告。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter()
    night = datetime(2026, 11, 11, 2, 0, 0, tzinfo=SHANGHAI)
    day = datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)
    assert limiter.in_night_silence(night) is True
    assert limiter.in_night_silence(day) is False
    allowed = limiter.allow(group=GROUP_A, content_hash="ann-1", now=night, kind="announcement")
    assert allowed is False, "夜间不得发送定时公告"


def test_rk06_same_content_cooldown():
    """RK-06 同内容在冷却期内不重发。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter(content_cooldown_seconds=600)
    now = datetime(2026, 11, 11, 12, 30, 0, tzinfo=SHANGHAI)
    h = "same-hash"
    assert limiter.allow(group=GROUP_A, content_hash=h, now=now, kind="reply") is True
    limiter.record(group=GROUP_A, content_hash=h, now=now, kind="reply")
    later = now + timedelta(minutes=2)
    assert limiter.allow(group=GROUP_A, content_hash=h, now=later, kind="reply") is False
    after = now + timedelta(minutes=11)
    assert limiter.allow(group=GROUP_A, content_hash=h, now=after, kind="reply") is True


def test_rk02_rk06_with_generated_sequences():
    """用 burst / 刷屏生成器灌入，限流必须生效。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter(group_per_minute=3, global_per_minute=8, content_cooldown_seconds=600)
    burst = generate_burst(n=40, interval_ms=10)
    now = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    allowed = 0
    for row in burst:
        if limiter.allow(group=row["group"], content_hash=row["id"], now=now, kind="reply"):
            limiter.record(group=row["group"], content_hash=row["id"], now=now, kind="reply")
            allowed += 1
    assert allowed <= 3, f"burst 同秒灌入单群放行 {allowed}，上限应为 3"

    limiter2 = impl.RateLimiter(group_per_minute=20, global_per_minute=40, content_cooldown_seconds=600)
    spam = generate_spam_repeat(n=20)
    # 同一文本应只放行首条
    h = "spam-same"
    sent = 0
    for i, row in enumerate(spam):
        t = now + timedelta(seconds=i)
        if limiter2.allow(group=row["group"], content_hash=h, now=t, kind="reply"):
            limiter2.record(group=row["group"], content_hash=h, now=t, kind="reply")
            sent += 1
    assert sent == 1, f"同文刷屏应只发 1 条，实际 {sent}"


def test_rk01_protocol_self_report(repo_root):
    """RK-01 协议自报。src/docs 仍为空时 skip；有代码但无自报则失败。"""
    src = repo_root / "src"
    docs = repo_root / "docs"
    config_dirs = [repo_root / "config", repo_root / "configs"]
    code_files = []
    if src.exists():
        code_files.extend([p for p in src.rglob("*") if p.is_file()])
    # 忽略空目录或占位
    code_files = [p for p in code_files if p.stat().st_size > 0]
    protocol_doc = None
    for cand in [
        docs / "PROTOCOL.md",
        repo_root / "PROTOCOL.md",
        docs / "protocol.md",
        docs / "ARCHITECTURE.md",
    ]:
        if cand.exists() and cand.stat().st_size > 0:
            text = cand.read_text(encoding="utf-8", errors="ignore")
            if any(
                k in text
                for k in ("协议", "Hook", "iPad", "Web 协议", "微信协议", "protocol")
            ):
                protocol_doc = cand
                break
    # 配置项 wechat.protocol
    config_hit = False
    for d in config_dirs:
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="ignore")
                if "wechat.protocol" in text or "protocol_type" in text:
                    config_hit = True
    if not code_files and not protocol_doc and not config_hit:
        pytest.skip(
            "开发尚未交付代码或协议自报（docs/PROTOCOL.md 或配置 wechat.protocol）。"
            "此项在有源码交付后为阻断：必须写明 Hook/Web/iPad 等协议及风险。"
        )
    assert protocol_doc or config_hit, (
        "已有源码但未找到协议自报。请在 docs/PROTOCOL.md 或配置 wechat.protocol "
        "写明实际微信协议/框架（Hook、Web、iPad、Windows/Mac 等）及已知风险。"
    )


# ---------------------------------------------------------------------------
# 每小时上限（RL-01～RL-06）：默认 40 条/小时，滑动窗口，重启保持，与拟人延迟相容。
# 超限策略（已写入 ACCEPTANCE.md）：自动回复降级为不回（drop），不排队。
# ---------------------------------------------------------------------------


def _limiter(**kwargs):
    impl = require_impl("RateLimiter")
    return impl.RateLimiter(**kwargs)


def test_rl01_default_hourly_cap_is_40():
    """RL-01 默认（或可配）全局每小时上限为 40。"""
    impl = require_impl("RateLimiter")
    limiter = impl.RateLimiter(group_per_minute=100, global_per_minute=100)
    cap = getattr(limiter, "global_per_hour", None)
    if cap is None:
        pytest.fail(
            "RateLimiter 未实现每小时上限（期望属性 global_per_hour，默认 40）。"
            "请在 src/bot_api.RateLimiter 增加滑动窗口全局 40 条/小时（可配）与单群上限。"
            "超限时自动回复应降级为不回（drop），不要排队。"
        )
    assert cap == 40 or cap <= 40, f"默认每小时上限应为 40，实际 {cap}"


def test_rl02_over_limit_is_dropped():
    """RL-02 超过每小时上限后不再放行（降级为不回，不排队爆发）。"""
    limiter = _limiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
    )
    now = datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    allowed = 0
    for i in range(50):
        h = f"hr-{i}"
        if limiter.allow(group=GROUP_A, content_hash=h, now=now + timedelta(seconds=i), kind="reply"):
            limiter.record(group=GROUP_A, content_hash=h, now=now + timedelta(seconds=i), kind="reply")
            allowed += 1
    assert allowed == 40, f"1 小时窗内应放行 40 条，实际 {allowed}（超限应丢弃/不回，不得放行 41+）"
    # 再要一条，仍应拒绝
    assert limiter.allow(group=GROUP_A, content_hash="hr-more", now=now + timedelta(seconds=50), kind="reply") is False


def test_rl03_sliding_window_boundary():
    """RL-03 滑动窗口：恰好满 1 小时后最早一条过期，才能再放行 1 条。不是整点清零。"""
    limiter = _limiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
    )
    t0 = datetime(2026, 11, 11, 11, 0, 0, tzinfo=SHANGHAI)
    for i in range(40):
        ts = t0 + timedelta(seconds=i)
        assert limiter.allow(group=GROUP_A, content_hash=f"b-{i}", now=ts, kind="reply")
        limiter.record(group=GROUP_A, content_hash=f"b-{i}", now=ts, kind="reply")
    # 1 小时不到：仍拒绝
    almost = t0 + timedelta(hours=1, seconds=-1)
    assert limiter.allow(group=GROUP_A, content_hash="b-almost", now=almost, kind="reply") is False
    # 第一条发出时间 t0 已滑出窗口
    opened = t0 + timedelta(hours=1, seconds=1)
    assert limiter.allow(group=GROUP_A, content_hash="b-open", now=opened, kind="reply") is True


def test_rl04_cross_hour_is_sliding_not_calendar():
    """RL-04 跨整点（11:30 的发送在 12:15 仍计入窗口）。"""
    limiter = _limiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=2,
        group_per_hour=2,
        content_cooldown_seconds=0,
    )
    t1 = datetime(2026, 11, 11, 11, 30, 0, tzinfo=SHANGHAI)
    t2 = datetime(2026, 11, 11, 11, 50, 0, tzinfo=SHANGHAI)
    assert limiter.allow(group=GROUP_A, content_hash="x1", now=t1, kind="reply")
    limiter.record(group=GROUP_A, content_hash="x1", now=t1, kind="reply")
    assert limiter.allow(group=GROUP_A, content_hash="x2", now=t2, kind="reply")
    limiter.record(group=GROUP_A, content_hash="x2", now=t2, kind="reply")
    noon = datetime(2026, 11, 11, 12, 15, 0, tzinfo=SHANGHAI)
    assert limiter.allow(group=GROUP_A, content_hash="x3", now=noon, kind="reply") is False, (
        "滑动窗口下 11:30 的发送在 12:15 仍应计入，不得因跨小时而清零"
    )
    later = datetime(2026, 11, 11, 12, 31, 0, tzinfo=SHANGHAI)
    assert limiter.allow(group=GROUP_A, content_hash="x4", now=later, kind="reply") is True


def test_rl05_restart_keeps_hourly_window():
    """RL-05 重启后小时窗口应保持（状态需可持久化到 store）。"""
    impl = require_impl("RateLimiter")
    store = {}
    now = datetime(2026, 11, 11, 16, 0, 0, tzinfo=SHANGHAI)
    try:
        a = impl.RateLimiter(
            group_per_minute=200,
            global_per_minute=200,
            global_per_hour=40,
            group_per_hour=40,
            content_cooldown_seconds=0,
            store=store,
        )
    except TypeError:
        pytest.fail(
            "RateLimiter 构造不接受 store=，重启后小时窗口无法保持。"
            "请把滑动窗口事件持久化（例如传入共享 store 或落盘），避免重启后再放行 40 条。"
        )
    for i in range(40):
        ts = now + timedelta(seconds=i)
        assert a.allow(group=GROUP_A, content_hash=f"rs-{i}", now=ts, kind="reply")
        a.record(group=GROUP_A, content_hash=f"rs-{i}", now=ts, kind="reply")
    b = impl.RateLimiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
        store=store,
    )
    assert b.allow(group=GROUP_A, content_hash="rs-new", now=now + timedelta(seconds=41), kind="reply") is False, (
        "重启后应仍视为已达 40 条/小时上限"
    )


def test_rl06_global_and_per_group_and_delay_compatible():
    """RL-06 全局与单群上限同时生效；与拟人延迟相容（在延迟之后仍检查窗口）。"""
    limiter = _limiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=5,
        group_per_hour=3,
        content_cooldown_seconds=0,
    )
    now = datetime(2026, 11, 11, 18, 0, 0, tzinfo=SHANGHAI)
    # 单群先打满 3
    for i in range(3):
        ts = now + timedelta(seconds=i)
        assert limiter.allow(group=GROUP_A, content_hash=f"ga-{i}", now=ts, kind="reply")
        limiter.record(group=GROUP_A, content_hash=f"ga-{i}", now=ts, kind="reply")
    assert limiter.allow(group=GROUP_A, content_hash="ga-x", now=now + timedelta(seconds=3), kind="reply") is False
    # 另一群还可发，但全局上限 5
    for i in range(2):
        ts = now + timedelta(seconds=10 + i)
        assert limiter.allow(group=GROUP_B, content_hash=f"gb-{i}", now=ts, kind="reply")
        limiter.record(group=GROUP_B, content_hash=f"gb-{i}", now=ts, kind="reply")
    assert limiter.allow(group=GROUP_B, content_hash="gb-x", now=now + timedelta(seconds=20), kind="reply") is False

    # 拟人延迟：先拿到 delay，时钟拨到 delay 之后再 allow，窗口判断用发送时刻
    impl = require_impl("RateLimiter")
    if hasattr(impl, "humanize_delay"):
        rng = __import__("random").Random(1)
        delay = impl.humanize_delay("请问手续费怎么算？", rng=rng)
    else:
        delay = limiter.next_delay()
    assert delay >= 1.5
    send_at = now + timedelta(seconds=delay)
    # 此时全局已满，延迟结束后仍应拒绝，避免「先占位再延迟」超发
    assert limiter.allow(group=GROUP_B, content_hash="after-delay", now=send_at, kind="reply") is False

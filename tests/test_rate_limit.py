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

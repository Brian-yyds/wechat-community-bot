"""全局每小时回复上限。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.bot_api import RateLimiter
from src.clock import FakeClock
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter as TokenLimiter


def test_bot_api_global_per_hour():
    limiter = RateLimiter(group_per_minute=100, global_per_minute=100, global_per_hour=40)
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    allowed = 0
    for i in range(50):
        h = f"h-{i}"
        if limiter.allow(group="g", content_hash=h, now=now, kind="reply"):
            limiter.record(group="g", content_hash=h, now=now, kind="reply")
            allowed += 1
    assert allowed == 40


def test_token_limiter_hourly_window(clock: FakeClock):
    limiter = TokenLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(100, 100),
            per_group=BucketSpec(100, 100),
            announcement=BucketSpec(100, 100),
            auto_reply=BucketSpec(100, 100),
            global_replies_per_hour=3,
        ),
        clock=clock,
    )
    assert limiter.try_acquire("g1", "auto_reply") is True
    assert limiter.try_acquire("g1", "auto_reply") is True
    assert limiter.try_acquire("g1", "auto_reply") is True
    assert limiter.try_acquire("g1", "auto_reply") is False
    # 公告不受每小时回复上限约束
    assert limiter.try_acquire("g1", "announcement") is True
    clock.advance(3601)
    assert limiter.try_acquire("g1", "auto_reply") is True

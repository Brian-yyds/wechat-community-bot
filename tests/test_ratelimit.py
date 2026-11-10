"""限速与拟人发送：令牌桶、触发限速、随机延迟、夜间静默、优先级队列。"""

from __future__ import annotations

from datetime import datetime, time

from src.adapters.mock import MockAdapter
from src.clock import FakeClock
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter, TokenBucket
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig, SendKind, _in_night


def _cfg(
    *,
    global_cap=10,
    global_refill=10,
    group_cap=10,
    group_refill=10,
    ann_cap=10,
    ann_refill=10,
    reply_cap=10,
    reply_refill=10,
) -> RateLimitConfig:
    return RateLimitConfig(
        global_bucket=BucketSpec(global_cap, global_refill),
        per_group=BucketSpec(group_cap, group_refill),
        announcement=BucketSpec(ann_cap, ann_refill),
        auto_reply=BucketSpec(reply_cap, reply_refill),
    )


def test_token_bucket_allow_then_block_then_refill(clock: FakeClock):
    bucket = TokenBucket(BucketSpec(capacity=2, refill_per_second=1), clock=clock)
    assert bucket.allow() is True
    assert bucket.allow() is True
    assert bucket.allow() is False
    clock.advance(1)
    assert bucket.allow() is True


def test_rate_limiter_per_group_and_global_and_quota(clock: FakeClock):
    limiter = RateLimiter(
        _cfg(global_cap=2, global_refill=0, group_cap=2, group_refill=0, reply_cap=1, reply_refill=0, ann_cap=2, ann_refill=0),
        clock=clock,
    )
    assert limiter.try_acquire("g1", "auto_reply") is True
    # auto_reply 配额已空
    assert limiter.try_acquire("g1", "auto_reply") is False
    # 公告配额仍可用，且全局还剩 1
    assert limiter.try_acquire("g1", "announcement") is True
    # 全局已空
    assert limiter.try_acquire("g2", "announcement") is False


def test_humanized_sender_delay_within_range_and_typing(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(_cfg(), clock=clock)
    rng = __import__("random").Random(1)
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=1.0,
            send_delay_max=2.0,
            typing_hook=True,
            typing_duration_min=0.5,
            typing_duration_max=0.5,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
        rng=rng,
    )
    start = clock.now()
    sender.submit_auto_reply("demo-group", "您好")
    assert sender.pump() == 1
    elapsed = (clock.now() - start).total_seconds()
    # 发送延迟 [1,2] + 正在输入 0.5
    assert 1.5 <= elapsed <= 2.5 + 1e-6
    assert adapter.sent_messages[0].text == "您好"
    assert adapter.typing_events and adapter.typing_events[0][0] == "demo-group"


def test_rate_limit_triggers_spacing(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    # 容量 1，每 5 秒补充 1 个
    limiter = RateLimiter(
        _cfg(global_cap=1, global_refill=0.2, group_cap=10, group_refill=10, reply_cap=10, reply_refill=10),
        clock=clock,
    )
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=0,
            send_delay_max=0,
            typing_hook=False,
            typing_duration_min=0,
            typing_duration_max=0,
            max_rate_wait_seconds=30,
        ),
        clock=clock,
        rng=__import__("random").Random(0),
    )
    sender.submit_auto_reply("g1", "第一条")
    sender.submit_auto_reply("g1", "第二条")
    t0 = clock.now()
    assert sender.pump() == 2
    # 第二条必须等到令牌恢复，间隔约 5 秒
    assert (clock.now() - t0).total_seconds() >= 5
    assert [s.text for s in adapter.sent_messages] == ["第一条", "第二条"]


def test_night_silent_drop_auto_reply_and_hold_announcement(clock: FakeClock):
    clock.set(datetime(2026, 11, 11, 23, 30, 0))
    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(_cfg(), clock=clock)
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=0,
            send_delay_max=0,
            typing_hook=False,
            night=NightSilentConfig(
                enabled=True,
                start=time(23, 0),
                end=time(7, 0),
                announcement_policy="hold",
                auto_reply_policy="drop",
            ),
        ),
        clock=clock,
    )
    sender.submit_auto_reply("g1", "自动回复应丢弃")
    sender.submit_announcement("g1", "公告应暂存")
    sent = sender.pump()
    assert sent == 0
    assert sender.dropped_count == 1
    assert sender.pending_size() == 1
    assert adapter.sent_messages == []
    # 白天再泵出公告
    clock.set(datetime(2026, 11, 12, 8, 0, 0))
    assert sender.pump() == 1
    assert adapter.sent_messages[0].text == "公告应暂存"


def test_priority_announcement_before_auto_reply(clock: FakeClock):
    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(_cfg(), clock=clock)
    sender = HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=0,
            send_delay_max=0,
            typing_hook=False,
        ),
        clock=clock,
    )
    sender.submit_auto_reply("g1", "回复先入队")
    sender.submit_announcement("g1", "公告后入队但优先")
    sender.pump()
    assert [s.text for s in adapter.sent_messages] == ["公告后入队但优先", "回复先入队"]


def test_in_night_ranges():
    assert _in_night(time(23, 30), time(23, 0), time(7, 0)) is True
    assert _in_night(time(6, 59), time(23, 0), time(7, 0)) is True
    assert _in_night(time(7, 0), time(23, 0), time(7, 0)) is False
    assert _in_night(time(12, 0), time(23, 0), time(7, 0)) is False
    assert _in_night(time(2, 0), time(1, 0), time(3, 0)) is True

# -*- coding: utf-8 -*-
"""限频：默认 40/小时、群 20/小时、全局 8/分钟、群 3/分钟；窗口、重启、超限、公告占用、拟人共同作用。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.clock import FakeClock
from src.main import build_rate_limiter
from src.ratelimit.sender import HumanizeConfig, NightSilentConfig

from .helpers import attach_logger, default_windows_limiter, make_sender, make_app


def test_builder_defaults_match_acceptance(clock: FakeClock):
    limiter, humanize = build_rate_limiter({}, clock)
    assert limiter.config.global_replies_per_hour == 40
    assert limiter.config.group_replies_per_hour == 20
    assert limiter.config.global_per_minute == 8
    assert limiter.config.group_per_minute == 3
    assert humanize.send_delay_min == 2.0
    assert humanize.send_delay_max == 6.0


def test_group_per_minute_sliding_boundary(clock: FakeClock):
    limiter = default_windows_limiter(clock)
    t0 = clock.now()
    assert limiter.try_acquire("g1", "auto_reply", t0) is True
    assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=10)) is True
    assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=20)) is True
    # 第 4 条仍在 60s 窗内，应拒绝
    assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=59)) is False
    # 恰好滑出第一条（t0 + 60s，cutoff = t0）应释放一个名额
    assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=60)) is True


def test_global_per_minute_across_groups(clock: FakeClock):
    limiter = default_windows_limiter(clock)
    now = clock.now()
    for i in range(8):
        assert limiter.try_acquire(f"g{i}", "auto_reply", now) is True
    assert limiter.try_acquire("gX", "auto_reply", now) is False
    assert limiter.try_acquire("gX", "auto_reply", now + timedelta(seconds=60)) is True


def test_group_and_global_hourly_limits(clock: FakeClock):
    # 把每分钟上限放宽，专门验证小时窗；间隔 >60s，避免被分钟窗误伤。
    limiter = default_windows_limiter(clock, global_per_minute=1000, group_per_minute=1000)
    t0 = clock.now()
    for i in range(20):
        assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=90 * i)) is True
    assert limiter.try_acquire("g1", "auto_reply", t0 + timedelta(seconds=90 * 20)) is False
    for i in range(20):
        assert limiter.try_acquire("g2", "auto_reply", t0 + timedelta(seconds=90 * i + 1)) is True
    assert limiter.try_acquire("g2", "auto_reply", t0 + timedelta(seconds=90 * 20 + 1)) is False
    assert limiter.try_acquire("g3", "auto_reply", t0 + timedelta(seconds=90 * 20 + 1)) is False


def test_hourly_window_crosses_clock_hour(clock: FakeClock):
    """滑动窗口应跨整点：10:30 发出 40 条，11:00 仍应受阻，11:30 首条滑出后才释放。"""
    limiter = default_windows_limiter(clock, global_per_minute=1000, group_per_minute=1000)
    start = datetime(2026, 11, 11, 10, 30, 0)
    clock.set(start)
    for i in range(40):
        gid = "ga" if i < 20 else "gb"
        # 全部落在 10:30–10:31，避免分钟窗干扰已放宽；小时窗从 10:30 起算。
        assert limiter.try_acquire(gid, "auto_reply", start + timedelta(seconds=i)) is True
    assert limiter.try_acquire("gc", "auto_reply", datetime(2026, 11, 11, 11, 0, 0)) is False
    assert limiter.try_acquire("gc", "auto_reply", datetime(2026, 11, 11, 11, 29, 59)) is False
    assert limiter.try_acquire("gc", "auto_reply", datetime(2026, 11, 11, 11, 30, 0)) is True


def test_time_until_allow_should_include_sliding_windows(clock: FakeClock):
    """超限后 time_until_allow 应给出等到窗口滑出的时间，而不是 0。"""
    limiter = default_windows_limiter(clock)
    now = clock.now()
    for _ in range(3):
        assert limiter.try_acquire("g1", "auto_reply", now) is True
    assert limiter.try_acquire("g1", "auto_reply", now) is False
    wait = limiter.time_until_allow("g1", "auto_reply", now)
    assert wait > 0, (
        f"单群每分钟已满时 time_until_allow 应为正数（等到窗口滑出），实际为 {wait}。"
        "当前实现只看令牌桶，不把滑动窗口算进等待时间。"
    )
    assert wait <= 60.0


def test_over_limit_is_queued_not_sent_and_should_be_logged(tmp_path: Path, clock: FakeClock):
    logger, handler = attach_logger("wechat_bot.review_e.rate")
    limiter = default_windows_limiter(clock)
    sender, adapter, _, _ = make_sender(
        clock, tmp_path, "reply", limiter=limiter, logger=logger
    )
    for i in range(5):
        sender.submit_auto_reply("g1", f"r{i}")
    sent = sender.pump()
    assert sent == 3, f"单群每分钟上限 3，实际发出 {sent}"
    assert len(adapter.sent_messages) == 3
    assert sender.pending_size() >= 1, "超限的自动回复应当排队而不是发出"
    joined = "\n".join(handler.records)
    assert any("限" in rec or "rate" in rec.lower() or "上限" in rec for rec in handler.records), (
        f"超限应记日志，实际日志为：{joined!r}"
    )


def test_announcements_do_not_consume_auto_reply_windows(clock: FakeClock):
    """现状确认：公告走独立令牌桶，不占用自动回复的 40/小时、20/小时、8/分钟、3/分钟窗口。"""
    limiter = default_windows_limiter(clock, global_per_minute=1000, group_per_minute=1000)
    now = clock.now()
    for i in range(20):
        assert limiter.try_acquire("g1", "auto_reply", now + timedelta(seconds=i)) is True
    assert limiter.try_acquire("g1", "auto_reply", now + timedelta(seconds=20)) is False
    for i in range(4):
        assert limiter.try_acquire("g1", "announcement", now) is True, (
            "公告不应被自动回复窗口拦截（令牌桶容量默认 4）"
        )


def test_rate_limiter_state_should_persist_across_restart(tmp_path: Path, clock: FakeClock):
    """验收：重启后限频应保持，避免重启即可再发满额。"""
    state = tmp_path / "rate_limit_window.json"
    first = default_windows_limiter(
        clock, state_path=state, global_per_minute=1000, group_per_minute=1000
    )
    now = clock.now()
    for i in range(40):
        gid = "g1" if i < 20 else "g2"
        assert first.try_acquire(gid, "auto_reply", now + timedelta(seconds=i)) is True

    restarted = default_windows_limiter(
        clock, state_path=state, global_per_minute=1000, group_per_minute=1000
    )
    allowed = restarted.try_acquire("g3", "auto_reply", now + timedelta(seconds=39))
    assert allowed is False, (
        "重启后同一小时窗口内应继续受 40/小时 约束；"
        "当前 RateLimiter 未把滑动窗口持久化到 state_path，重启后可立刻再发 40 条。"
    )


def test_humanize_and_rate_limit_together_do_not_exceed(tmp_path: Path, clock: FakeClock):
    humanize = HumanizeConfig(
        send_delay_min=2.0,
        send_delay_max=2.0,
        typing_hook=False,
        night=NightSilentConfig(enabled=False),
        max_rate_wait_seconds=30,
    )
    limiter = default_windows_limiter(clock)
    sender, adapter, _, _ = make_sender(
        clock, tmp_path, "reply", limiter=limiter, humanize=humanize
    )
    for i in range(6):
        sender.submit_auto_reply("g1", f"h{i}")
    sender.pump()
    # 即便拟人延迟把时钟拨快了 2s * n，一分钟窗口内仍不得超过 3 条（除非已滑出）。
    # 6 条 * 2s = 12s，仍在同一分钟内。
    assert len(adapter.sent_messages) == 3
    elapsed = (clock.now() - datetime(2026, 11, 11, 10, 0, 0)).total_seconds()
    assert elapsed < 60
    # 再等到窗口外应能继续发
    clock.advance(60)
    sender.pump()
    assert len(adapter.sent_messages) >= 4


def test_announcement_token_bucket_independent_of_reply_hour_cap(tmp_path: Path, clock: FakeClock):
    limiter = default_windows_limiter(clock, global_per_minute=1000, group_per_minute=1000)
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply", limiter=limiter)
    now = clock.now()
    for i in range(20):
        assert limiter.try_acquire("g1", "auto_reply", now + timedelta(seconds=i)) is True
    sender.submit_announcement("g1", "满额后的公告")
    assert sender.pump() == 1
    assert adapter.sent_messages[0].text == "满额后的公告"

# -*- coding: utf-8 -*-
"""复测 3：time_until_allow 计入滑动窗口；超限继续排队并记脱敏日志。"""

from __future__ import annotations

from pathlib import Path

from src.clock import FakeClock

from .helpers import attach_logger, default_windows_limiter, make_sender


def test_retest_time_until_allow_includes_minute_and_hour_windows(clock: FakeClock):
    limiter = default_windows_limiter(clock)
    now = clock.now()
    for _ in range(3):
        assert limiter.try_acquire("g1", "auto_reply", now) is True
    assert limiter.try_acquire("g1", "auto_reply", now) is False
    wait = limiter.time_until_allow("g1", "auto_reply", now)
    assert wait > 0, f"单群每分钟已满时等待应为正数，实际 {wait}"
    assert wait <= 60.0

    # 小时窗：放宽分钟上限后打满 40 条，等待应接近 3600s。
    hour = default_windows_limiter(clock, global_per_minute=1000, group_per_minute=1000)
    t0 = clock.now()
    for i in range(40):
        gid = "ga" if i < 20 else "gb"
        assert hour.try_acquire(gid, "auto_reply", t0) is True
    assert hour.try_acquire("gc", "auto_reply", t0) is False
    hour_wait = hour.time_until_allow("gc", "auto_reply", t0)
    assert hour_wait > 0, f"全局每小时已满时等待应为正数，实际 {hour_wait}"
    assert hour_wait <= 3600.0
    # 公告不占用自动回复窗口，等待应为 0（或仅令牌桶）。
    assert hour.time_until_allow("gc", "announcement", t0) == 0.0 or hour.try_acquire("gc", "announcement", t0)


def test_retest_over_limit_stays_queued_and_logs_redacted(tmp_path: Path, clock: FakeClock):
    logger, handler = attach_logger("wechat_bot.review_e.retest_rate")
    limiter = default_windows_limiter(clock)
    sender, adapter, _, _ = make_sender(clock, tmp_path, "reply", limiter=limiter, logger=logger)
    # 用微信号形态的群 id，确认超限日志会脱敏。
    group = "wxid_hiddengroup01"
    for i in range(5):
        sender.submit_auto_reply(group, f"r{i}")
    sent = sender.pump()
    assert sent == 3, f"单群每分钟上限 3，实际发出 {sent}"
    assert len(adapter.sent_messages) == 3
    assert sender.pending_size() >= 1, "超限的自动回复应当继续排队而不是发出或丢弃"
    joined = "\n".join(handler.records)
    assert any("限" in rec or "上限" in rec or "rate" in rec.lower() for rec in handler.records), (
        f"超限应记日志，实际：{joined!r}"
    )
    assert any(
        ("等" in rec or "wait" in rec.lower() or "秒" in rec) for rec in handler.records
    ), f"超限日志应包含需等待的时长，实际：{joined!r}"
    assert "wxid_hiddengroup01" not in joined, f"超限日志未脱敏群 id：{joined!r}"
    # 再等到窗口外应能继续发出排队中的消息。
    clock.advance(60)
    sender.pump()
    assert len(adapter.sent_messages) >= 4

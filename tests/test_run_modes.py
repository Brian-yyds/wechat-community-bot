"""运行模式 observe / announce / reply，以及紧急暂停 PAUSE 文件。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from random import Random

import pytest

from src.adapters.mock import MockAdapter
from src.adapters.pause import PauseGate
from src.clock import FakeClock
from src.config_util import ConfigError, load_config
from src.logging_util import RedactFilter
from src.main import build_app, build_rate_limiter
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig
from src.run_mode import RunMode, RunModeError, SendGate, parse_run_mode


def _fast_humanize() -> HumanizeConfig:
    return HumanizeConfig(
        send_delay_min=0,
        send_delay_max=0,
        open_session_delay_min=0,
        open_session_delay_max=0,
        announcement_delay_min=0,
        announcement_delay_max=0,
        typing_hook=False,
        night=NightSilentConfig(enabled=False),
    )


def _sender(clock: FakeClock, tmp_path: Path, mode: str, paused: bool = False) -> tuple[HumanizedSender, MockAdapter, PauseGate]:
    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(100, 100),
            per_group=BucketSpec(100, 100),
            announcement=BucketSpec(100, 100),
            auto_reply=BucketSpec(100, 100),
            global_replies_per_hour=40,
        ),
        clock=clock,
    )
    gate = PauseGate(data_dir=tmp_path)
    if paused:
        gate.pause("测试")
    sender = HumanizedSender(
        adapter,
        limiter,
        config=_fast_humanize(),
        clock=clock,
        rng=Random(0),
        send_gate=SendGate(mode=mode, pause_gate=gate),
    )
    return sender, adapter, gate


def _app(tmp_path: Path, repo_root: Path, clock: FakeClock, mode: str | None = None) -> dict:
    config = load_config(repo_root / "config" / "example.toml", project_root=repo_root)
    config = dict(config)
    if mode is not None:
        config["run"] = {"mode": mode}
    rate = dict(config.get("rate_limit") or {})
    rate["send_delay_min"] = 0
    rate["send_delay_max"] = 0
    rate["typing_hook"] = False
    night = dict(rate.get("night_silent") or {})
    night["enabled"] = False
    rate["night_silent"] = night
    config["rate_limit"] = rate
    return build_app(config, project_root=repo_root, clock=clock, rng=Random(1), data_dir=tmp_path)


def test_default_mode_is_observe():
    assert parse_run_mode(None) is RunMode.OBSERVE
    assert parse_run_mode("") is RunMode.OBSERVE
    assert parse_run_mode("REPLY") is RunMode.REPLY


def test_invalid_mode_raises():
    with pytest.raises(RunModeError, match="未知运行模式"):
        parse_run_mode("broadcast")


def test_invalid_mode_in_config_falls_back_to_observe(tmp_path: Path, repo_root: Path, clock: FakeClock):
    """非法 run.mode 默认安全：回退到 observe 继续只读运行，而不是拒绝启动或落到 reply。"""
    app = _app(tmp_path, repo_root, clock, mode="weird")
    assert app["run_mode"] is RunMode.OBSERVE
    assert app["send_gate"].get_mode() == "observe"


def test_example_config_default_is_observe(repo_root: Path, clock: FakeClock, tmp_path: Path):
    app = _app(tmp_path, repo_root, clock, mode=None)
    assert app["run_mode"] is RunMode.OBSERVE


def test_observe_drops_reply_and_announcement_at_send_exit(clock: FakeClock, tmp_path: Path):
    sender, adapter, _ = _sender(clock, tmp_path, "observe")
    # 上游仍然提交，出口必须丢弃。
    sender.submit_auto_reply("g1", "不应发出的回复")
    sender.submit_announcement("g1", "不应发出的公告")
    assert sender.pump() == 0
    assert sender.dropped_count == 2
    assert adapter.sent_messages == []


def test_announce_allows_announcement_but_drops_auto_reply(clock: FakeClock, tmp_path: Path):
    sender, adapter, _ = _sender(clock, tmp_path, "announce")
    sender.submit_auto_reply("g1", "自动回复应丢弃")
    sender.submit_announcement("g1", "公告应发出")
    assert sender.pump() == 1
    assert sender.dropped_count == 1
    assert [m.text for m in adapter.sent_messages] == ["公告应发出"]


def test_reply_allows_both(clock: FakeClock, tmp_path: Path):
    sender, adapter, _ = _sender(clock, tmp_path, "reply")
    sender.submit_auto_reply("g1", "回复")
    sender.submit_announcement("g1", "公告")
    assert sender.pump() == 2
    assert sender.dropped_count == 0
    assert [m.text for m in adapter.sent_messages] == ["公告", "回复"]


def test_end_to_end_modes_via_one_line_config(repo_root: Path, clock: FakeClock, tmp_path: Path):
    observe = _app(tmp_path / "obs", repo_root, clock, mode="observe")
    observe["adapter"].start()
    observe["adapter"].inject_message("你好，在吗？", sender_id="u1")
    observe["scheduler"].fire_now(observe["scheduler"].list_announcements()[0].id)
    observe["sender"].pump()
    observe["adapter"].stop()
    assert observe["adapter"].sent_messages == []

    announce = _app(tmp_path / "ann", repo_root, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="announce")
    announce["adapter"].start()
    announce["adapter"].inject_message("你好，在吗？", sender_id="u1")
    announce["scheduler"].fire_now(announce["scheduler"].list_announcements()[0].id)
    announce["sender"].pump()
    announce["adapter"].stop()
    texts = [m.text for m in announce["adapter"].sent_messages]
    assert texts, "announce 模式应发出定时公告"
    assert all("早上好" in t or "风险" in t for t in texts)
    assert not any("您好，我是群助手" in t for t in texts)

    reply = _app(tmp_path / "rep", repo_root, FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode="reply")
    reply["adapter"].start()
    reply["adapter"].inject_message("你好，在吗？", sender_id="u1")
    reply["scheduler"].fire_now(reply["scheduler"].list_announcements()[0].id)
    reply["sender"].pump()
    reply["adapter"].stop()
    texts = [m.text for m in reply["adapter"].sent_messages]
    assert any("您好，我是群助手" in t for t in texts)
    assert any("早上好" in t or "风险" in t for t in texts)


def test_pause_file_blocks_all_sends(clock: FakeClock, tmp_path: Path):
    sender, adapter, gate = _sender(clock, tmp_path, "reply", paused=True)
    assert (tmp_path / "PAUSE").exists()
    sender.submit_auto_reply("g1", "回复")
    sender.submit_announcement("g1", "公告")
    assert sender.pump() == 0
    assert adapter.sent_messages == []
    gate.resume()
    sender.submit_auto_reply("g1", "恢复后的回复")
    assert sender.pump() == 1
    assert adapter.sent_messages[0].text == "恢复后的回复"


def test_pause_checked_again_right_before_send(clock: FakeClock, tmp_path: Path):
    sender, adapter, gate = _sender(clock, tmp_path, "reply")
    # 拉长拟人延迟，使暂停文件能在等待期间被创建。
    sender.config.send_delay_min = 2.0
    sender.config.send_delay_max = 2.0
    sender.submit_auto_reply("g1", "延迟期间被暂停")

    # 不走 pump 的自动 sleep，而是手动派发前先放暂停文件。
    # 先入队，再创建 PAUSE，出口在发出前必须拦截。
    gate.pause("中途暂停")
    assert sender.pump() == 0
    assert adapter.sent_messages == []


def test_drop_is_logged_and_redacted(clock: FakeClock, tmp_path: Path):
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    logger = logging.getLogger("wechat_bot.test_run_modes")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addFilter(RedactFilter())
    handler = _Handler()
    handler.addFilter(RedactFilter())
    logger.addHandler(handler)
    logger.propagate = False

    adapter = MockAdapter(clock=clock)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(100, 100),
            per_group=BucketSpec(100, 100),
            announcement=BucketSpec(100, 100),
            auto_reply=BucketSpec(100, 100),
        ),
        clock=clock,
    )
    gate = SendGate(mode="observe", logger=logger)
    sender = HumanizedSender(
        adapter,
        limiter,
        config=_fast_humanize(),
        clock=clock,
        send_gate=gate,
        logger=logger,
    )
    # 群 id 中夹带 token，日志应被脱敏。
    sender.submit_auto_reply("token abcdef123456", "不该出现在日志里的正文")
    sender.pump()
    assert records
    joined = "\n".join(records)
    assert "observe" in joined or "已丢弃" in joined
    assert "abcdef123456" not in joined
    assert "不该出现在日志里的正文" not in joined


def test_rate_limit_defaults_from_example_and_builder(repo_root: Path, clock: FakeClock):
    config = load_config(repo_root / "config" / "example.toml", project_root=repo_root)
    limiter, humanize = build_rate_limiter(config, clock)
    assert limiter.config.global_replies_per_hour == 40
    assert limiter.config.group_replies_per_hour == 20
    assert limiter.config.global_per_minute == 8
    assert limiter.config.group_per_minute == 3
    assert humanize.send_delay_min == 2.0
    assert humanize.send_delay_max == 6.0
    assert humanize.night.enabled is True
    assert humanize.night.auto_reply_policy == "drop"

    # 缺省配置也应得到同样的默认值。
    empty_limiter, empty_human = build_rate_limiter({}, clock)
    assert empty_limiter.config.global_replies_per_hour == 40
    assert empty_limiter.config.group_replies_per_hour == 20
    assert empty_limiter.config.global_per_minute == 8
    assert empty_limiter.config.group_per_minute == 3
    assert empty_human.send_delay_min == 2.0
    assert empty_human.send_delay_max == 6.0
    assert empty_human.night.enabled is True


def test_rate_limits_are_configurable_and_enforced(clock: FakeClock):
    limiter, _ = build_rate_limiter(
        {
            "rate_limit": {
                "global_replies_per_hour": 2,
                "group_replies_per_hour": 1,
                "global_per_minute": 2,
                "group_per_minute": 1,
                "global": {"capacity": 100, "refill_per_second": 100},
                "per_group": {"capacity": 100, "refill_per_second": 100},
                "quotas": {
                    "announcement": {"capacity": 100, "refill_per_second": 100},
                    "auto_reply": {"capacity": 100, "refill_per_second": 100},
                },
            }
        },
        clock,
    )
    assert limiter.try_acquire("g1", "auto_reply") is True
    # 单群每分钟 / 每小时上限为 1
    assert limiter.try_acquire("g1", "auto_reply") is False
    # 换群后仍受全局每小时 2、每分钟 2 约束，还可以再发一条
    assert limiter.try_acquire("g2", "auto_reply") is True
    assert limiter.try_acquire("g2", "auto_reply") is False
    # 公告不受自动回复的小时/分钟窗口限制
    assert limiter.try_acquire("g1", "announcement") is True

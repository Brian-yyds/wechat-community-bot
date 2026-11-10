# -*- coding: utf-8 -*-
"""模块 E 补测公共装配：一律使用 tmp_path，不碰仓库 data/。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from random import Random

from src.adapters.mock import MockAdapter
from src.adapters.pause import PauseGate
from src.clock import FakeClock
from src.config_util import load_config
from src.logging_util import RedactFilter
from src.main import build_app
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig
from src.run_mode import SendGate

REPO_ROOT = Path(__file__).resolve().parents[2]


def fast_humanize() -> HumanizeConfig:
    return HumanizeConfig(
        send_delay_min=0,
        send_delay_max=0,
        typing_hook=False,
        night=NightSilentConfig(enabled=False),
        max_rate_wait_seconds=30,
    )


def generous_limiter(clock: FakeClock, **overrides) -> RateLimiter:
    cfg = dict(
        global_bucket=BucketSpec(100, 100),
        per_group=BucketSpec(100, 100),
        announcement=BucketSpec(100, 100),
        auto_reply=BucketSpec(100, 100),
        global_replies_per_hour=1000,
        group_replies_per_hour=1000,
        global_per_minute=1000,
        group_per_minute=1000,
    )
    cfg.update(overrides)
    return RateLimiter(RateLimitConfig(**cfg), clock=clock)


def default_windows_limiter(clock: FakeClock, **overrides) -> RateLimiter:
    state_path = overrides.pop("state_path", None)
    cfg = dict(
        global_bucket=BucketSpec(100, 100),
        per_group=BucketSpec(100, 100),
        announcement=BucketSpec(100, 100),
        auto_reply=BucketSpec(100, 100),
        global_replies_per_hour=40,
        group_replies_per_hour=20,
        global_per_minute=8,
        group_per_minute=3,
    )
    cfg.update(overrides)
    return RateLimiter(RateLimitConfig(**cfg), clock=clock, state_path=state_path)


def make_sender(
    clock: FakeClock,
    tmp_path: Path,
    mode: str = "reply",
    *,
    paused: bool = False,
    limiter: RateLimiter | None = None,
    humanize: HumanizeConfig | None = None,
    latency_store=None,
    logger: logging.Logger | None = None,
) -> tuple[HumanizedSender, MockAdapter, PauseGate, SendGate]:
    adapter = MockAdapter(clock=clock)
    gate = PauseGate(data_dir=tmp_path)
    if paused:
        gate.pause("review_e")
    send_gate = SendGate(mode=mode, pause_gate=gate, logger=logger)
    sender = HumanizedSender(
        adapter,
        limiter or generous_limiter(clock),
        config=humanize or fast_humanize(),
        clock=clock,
        rng=Random(0),
        send_gate=send_gate,
        latency_store=latency_store,
        logger=logger,
    )
    return sender, adapter, gate, send_gate


def make_app(
    tmp_path: Path,
    clock: FakeClock,
    mode: str | None = "reply",
    *,
    extra_config: dict | None = None,
) -> dict:
    config = load_config(REPO_ROOT / "config" / "example.toml", project_root=REPO_ROOT)
    config = dict(config)
    if mode is None:
        config.pop("run", None)
    else:
        config["run"] = {"mode": mode}
    rate = dict(config.get("rate_limit") or {})
    rate["send_delay_min"] = 0
    rate["send_delay_max"] = 0
    rate["typing_hook"] = False
    night = dict(rate.get("night_silent") or {})
    night["enabled"] = False
    rate["night_silent"] = night
    config["rate_limit"] = rate
    if extra_config:
        for key, value in extra_config.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                merged = dict(config[key])
                merged.update(value)
                config[key] = merged
            else:
                config[key] = value
    return build_app(
        config,
        project_root=REPO_ROOT,
        clock=clock,
        rng=Random(1),
        data_dir=tmp_path,
    )


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


def attach_logger(name: str) -> tuple[logging.Logger, ListHandler]:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.filters.clear()
    logger.addFilter(RedactFilter())
    handler = ListHandler()
    handler.addFilter(RedactFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger, handler

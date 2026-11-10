# -*- coding: utf-8 -*-
"""复测 6：首轮 real 模式遗留 4 项，以及 LT-02/04/05 是否硬写 1.0 的说明。"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
from random import Random

import pytest

from src.bot_api import RateLimiter as ApiRateLimiter
from src.bot_api import humanize_delay, humanize_delay_range
from src.clock import FakeClock
from src.humanize import DEFAULT_OPEN_SESSION, DEFAULT_OTHER
from src.latency import OPEN_SESSION_TARGET_S
from src.run_mode import RunMode, SendGate

from .helpers import REPO_ROOT, default_windows_limiter, make_app


def test_retest_open_session_rule_range_is_1_5_to_2_4_and_configurable():
    """开发取 1.5–2.4s，下限可配；以此为准。"""
    sig = inspect.signature(humanize_delay_range)
    assert "path" in sig.parameters
    lo, hi = humanize_delay_range("open_session_rule")
    assert (lo, hi) == (1.5, 2.4) == DEFAULT_OPEN_SESSION
    assert hi + 0.5 <= OPEN_SESSION_TARGET_S + 1e-9
    other = humanize_delay_range()
    assert other == DEFAULT_OTHER == (2.0, 6.0)
    # 下限可配：传入自定义 profiles。
    from src.humanize import HumanizeProfiles, humanize_delay_range as raw_range

    custom = HumanizeProfiles(open_session_rule=(1.2, 2.4), min_floor=1.0)
    clo, chi = raw_range("open_session_rule", profiles=custom)
    assert (clo, chi) == (1.2, 2.4)


def test_retest_lt020405_read_impl_range_not_hardcoded_1_0():
    """记录：LT-02/04/05 从 humanize_delay_range 读区间，没有把实现硬写成 1.0。

    tests/constants.py 里 HUMANIZE_OPEN_SESSION_DEFAULT 默认仍是 (1.0, 2.2)，
    test_latency.py 文档也写「建议 1.0–2.2s」，但 LT-02/04/05 本身用实现返回值
    和可配的 HUMANIZE_MIN_FLOOR（默认 0.8）/ 3s 预算做断言，不会因开发取 1.5–2.4
    而失败。属文档/常量口径不一致，不是会失败的硬编码。按要求不改已有文件。
    """
    latency = (REPO_ROOT / "tests" / "test_latency.py").read_text(encoding="utf-8")
    constants = (REPO_ROOT / "tests" / "constants.py").read_text(encoding="utf-8")
    # 三个用例都通过 _range_for / humanize_delay_range 读取实现。
    assert "def test_lt02_segmented_percentiles_open_session_rule" in latency
    assert "def test_lt04_humanize_compatible_with_path_targets" in latency
    assert "def test_lt05_tradeoff_is_documented" in latency
    assert "_range_for(impl, \"open_session_rule\")" in latency
    assert "impl.humanize_delay_range()" in latency
    # 没有出现把短区间断言成 (1.0, 2.2) 或 lo == 1.0。
    assert "assert short == (1.0, 2.2)" not in latency
    assert "assert lo == 1.0" not in latency
    assert "assert slo == 1.0" not in latency
    # 常量默认值仍写 1.0，供报告说明；LT 用例并未拿它去卡实现。
    assert 'BOT_TEST_HUMANIZE_OPEN_MIN", 1.0)' in constants
    assert "HUMANIZE_OPEN_SESSION_DEFAULT" in latency  # 仅 import，未用于断言实现区间
    # 确认 import 后没有被用来和实现短区间做相等比较。
    body = latency.split("def test_lt02_segmented_percentiles_open_session_rule", 1)[1]
    lt02_04_05 = body.split("# ---------------------------------------------------------------------------", 1)[0]
    # LT-04 在 LT-02 之前，一并检查文件前半。
    head = latency.split("# ---------------------------------------------------------------------------")[0]
    assert "HUMANIZE_OPEN_SESSION_DEFAULT" not in head.split("from constants import", 1)[-1] or (
        "HUMANIZE_OPEN_SESSION_DEFAULT" in latency.split("from constants import", 1)[1].split(")", 1)[0]
        and "HUMANIZE_OPEN_SESSION_DEFAULT" not in head.split(")", 1)[-1]
    )


def test_retest_humanize_delay_accepts_rng_and_path_seed_reproducible():
    """LT-08/09：humanize_delay(text, rng=, path=) 可注入种子，不真实睡眠。"""
    sig = inspect.signature(humanize_delay)
    assert "rng" in sig.parameters and "path" in sig.parameters
    a = [humanize_delay("abc", rng=Random(42), path="open_session_rule") for _ in range(10)]
    b = [humanize_delay("abc", rng=Random(42), path="open_session_rule") for _ in range(10)]
    assert a == b
    c = humanize_delay("请问现在开户需要准备哪些资料？", rng=Random(1))
    d = humanize_delay("你好", rng=Random(1))
    assert c > d
    lo, hi = humanize_delay_range("open_session_rule")
    sample = humanize_delay("你好", rng=Random(7), path="open_session_rule")
    assert lo - 1e-9 <= sample <= hi + 1e-9


def test_retest_rate_limiter_hourly_window_persists_after_restart(tmp_path: Path, clock: FakeClock):
    """RL-05：小时窗口落盘，重启后保持。"""
    state = tmp_path / "rate_limit_window.json"
    first = default_windows_limiter(
        clock, state_path=state, global_per_minute=1000, group_per_minute=1000
    )
    now = clock.now()
    for i in range(40):
        gid = "g1" if i < 20 else "g2"
        assert first.try_acquire(gid, "auto_reply", now + timedelta(seconds=i)) is True
    assert state.exists()
    restarted = default_windows_limiter(
        clock, state_path=state, global_per_minute=1000, group_per_minute=1000
    )
    assert restarted.try_acquire("g3", "auto_reply", now + timedelta(seconds=39)) is False

    # bot_api.RateLimiter 的 store= 路径同样要保持（RL-05 原用例）。
    store: dict = {}
    a = ApiRateLimiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
        store=store,
    )
    t0 = datetime(2026, 11, 11, 16, 0, 0)
    for i in range(40):
        ts = t0 + timedelta(seconds=i)
        assert a.allow(group="ga", content_hash=f"rs-{i}", now=ts, kind="reply")
        a.record(group="ga", content_hash=f"rs-{i}", now=ts, kind="reply")
    b = ApiRateLimiter(
        group_per_minute=200,
        global_per_minute=200,
        global_per_hour=40,
        group_per_hour=40,
        content_cooldown_seconds=0,
        store=store,
    )
    assert b.allow(group="ga", content_hash="rs-new", now=t0 + timedelta(seconds=41), kind="reply") is False


def test_retest_run_mode_restored_after_restart(tmp_path: Path):
    """RM-04：set_mode 持久化，重启后恢复，默认 observe 不得覆盖已写入的 reply。"""
    store: dict = {}
    gate = SendGate(mode="observe", store=store, state_path=tmp_path / "run_mode.json")
    gate.set_mode("reply")
    assert gate.get_mode() == "reply"
    restarted = SendGate(mode="observe", store=store, state_path=tmp_path / "run_mode.json")
    assert restarted.get_mode() == "reply"
    assert restarted.mode is RunMode.REPLY
    assert restarted.allow_auto_reply() is True

    app = make_app(tmp_path / "app", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode=None)
    app["send_gate"].set_mode("announce")
    again = make_app(tmp_path / "app", FakeClock(datetime(2026, 11, 11, 10, 0, 0)), mode=None)
    assert again["send_gate"].get_mode() == "announce"


def test_retest_open_session_range_consistent_in_constants_and_docs():
    """已打开会话短区间口径统一为 1.5–2.4 秒：常量、API 与主要文档一致。"""
    from src.bot_api import HUMANIZE_OPEN_SESSION, humanize_delay_range
    from src.humanize import DEFAULT_OPEN_SESSION

    lo, hi = humanize_delay_range("open_session_rule")
    assert (lo, hi) == (1.5, 2.4) == DEFAULT_OPEN_SESSION == HUMANIZE_OPEN_SESSION

    repo = Path(__file__).resolve().parents[2]
    docs = [
        repo / "docs" / "ARCHITECTURE.md",
        repo / "docs" / "WINDOWS.md",
        repo / "src" / "humanize.py",
        repo / "src" / "bot_api.py",
    ]
    missing = []
    for path in docs:
        body = path.read_text(encoding="utf-8")
        has_range = ("1.5–2.4" in body) or ("1.5-2.4" in body) or ("1.5–2.4 秒" in body)
        if not has_range:
            missing.append(path.name)
    assert missing == [], f"主要文档/常量出处未写明 1.5–2.4 秒短区间: {missing}"

# -*- coding: utf-8 -*-
"""复测：对照 C/D 首轮退回项与本次要求逐条验证。不改 src/，不触碰仓库 data/PAUSE。"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.adapters.base import EventType, Group, Message
from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.notification import (
    FakeNotificationSource,
    NotificationAdapter,
)
from src.adapters.ocr import (
    FakeScreenDriver,
    GroupNotConfirmedError,
    OcrOutboundAdapter,
    RateLimitedError,
    normalize_ui_text,
)
from src.adapters.pause import PauseGate, SendPausedError
from src.clock import FakeClock
from src.pipeline.pipeline import Pipeline
from src.run_mode import RunMode, SendGate
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizedSender


def _elements(group: str = "交易会员群-主群", confidence: float = 0.99, *, extra: list[dict] | None = None) -> list[dict]:
    items = [
        {"text": group, "x": 80, "y": 20, "width": 120, "height": 20, "confidence": confidence},
        {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": confidence},
        {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": confidence},
    ]
    if extra:
        items.extend(extra)
    return items


def _ocr_adapter(tmp_path: Path, clock: FakeClock, driver: FakeScreenDriver, **kwargs) -> OcrOutboundAdapter:
    kwargs.setdefault("limiter", None)
    kwargs.setdefault("enforce_rate_limit", False)
    kwargs.setdefault("data_dir", tmp_path)
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("group_names", {"g1": "交易会员群-主群"})
    kwargs.setdefault("input_pause_min", 0.0)
    kwargs.setdefault("input_pause_max", 0.0)
    kwargs.setdefault("pause", PauseGate(data_dir=tmp_path))
    # 机械用例（群名、小时上限等）需要直发；无闸门默认拒发与运行模式用例显式关闭此开关。
    kwargs.setdefault("allow_ungated_for_tests", True)
    return OcrOutboundAdapter(driver=driver, **kwargs)


def test_wake_a_on_preview_false_does_not_connect_to_a(clock: FakeClock):
    class Capture:
        def __init__(self) -> None:
            self.wakes: list[object] = []

        def request_immediate_poll(self, hint=None):
            self.wakes.append(hint)

    adapter = build_adapter(
        {
            "wechat": {"inbound": "mock", "outbound": "mock"},
            "inbound": {
                "notification": {
                    "enabled": True,
                    "source": "fake",
                    "wake_a_on_preview": False,
                }
            },
        },
        groups=[Group("g1", "交易会员群-主群")],
        clock=clock,
    )
    assert isinstance(adapter, CompositeAdapter)
    assert adapter.notification is not None
    assert adapter.notification.wake_callback is None
    assert adapter.wake_target is None

    capture = Capture()
    adapter.inbound = capture  # type: ignore[assignment]
    source = FakeNotificationSource(clock=clock)
    source.push("交易会员群-主群", "预览正文", ts="2026-11-11T12:00:00")
    adapter.notification.source = source  # 使用已构造适配器的源
    # 没有回调时，poll 不得去唤 A
    adapter.notification.poll_once()
    assert capture.wakes == []


def test_partial_preview_is_not_auto_replied(clock: FakeClock):
    class Recorder:
        def __init__(self) -> None:
            self.sent = []

        def submit_auto_reply(self, group_id, text, **kwargs) -> None:
            self.sent.append((group_id, text))

    sender = Recorder()
    pipe = Pipeline(sender=sender, clock=clock)
    msg = Message(
        message_id="notify-1",
        group_id="g1",
        sender_id="u1",
        content="请问开户要哪些资料",
        timestamp=clock.now(),
        sender_name="甲",
        extra={"partial": True, "source": "notification", "complete": False},
    )
    result = pipe.process(msg)
    assert result.skipped is True
    assert result.skip_reason == "partial_preview"
    assert result.reply_text is None
    assert sender.sent == []


def test_notification_throttle_from_config_merges_and_caps(clock: FakeClock):
    adapter = NotificationAdapter.from_config(
        {"inbound": {"notification": {"source": "fake", "enabled": True}}},
        clock=clock,
    )
    assert adapter.wake_budget_per_poll == 20
    assert adapter.merge_wake_by_group is True
    source = FakeNotificationSource(clock=clock)
    adapter.source = source
    woken: list[str] = []
    adapter.set_wake_callback(lambda h: woken.append(h.group))
    base = datetime(2026, 11, 11, 13, 0, 0)
    # 3 个群各 10 条，共 30 条预览；按群合并后 3 次唤醒，且不超过预算 20。
    for i in range(30):
        source.push(f"群{i % 3}", f"预览{i}", ts=base + timedelta(seconds=i))
    hints = adapter.poll_once()
    assert len(hints) == 30
    assert all(h.partial is True and h.complete is False for h in hints)
    assert woken == ["群0", "群1", "群2"]


def test_notification_flood_budget_limits_wakes(clock: FakeClock):
    source = FakeNotificationSource(clock=clock)
    woken: list[str] = []
    adapter = NotificationAdapter(
        source=source,
        clock=clock,
        wake_callback=lambda h: woken.append(h.group),
        wake_budget_per_poll=5,
        merge_wake_by_group=False,
    )
    base = datetime(2026, 11, 11, 14, 0, 0)
    for i in range(40):
        source.push(f"群{i}", f"预览{i}", ts=base + timedelta(seconds=i))
    hints = adapter.poll_once()
    assert len(hints) == 40
    assert len(woken) == 5
    assert all(h.complete is False for h in hints)


def test_factory_warns_that_notification_is_not_sole_source(clock: FakeClock):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        adapter = build_adapter(
            {"wechat": {"inbound": "notification", "outbound": "mock"}},
            groups=[Group("g1", "演示群")],
            clock=clock,
        )
    texts = " ".join(str(w.message) for w in caught)
    assert "唯一消息源" in texts or "不能作为唯一" in texts
    # 即使被配成主 inbound，预览事件仍标记 partial，且声明不完整。
    inbound = adapter.inbound if isinstance(adapter, CompositeAdapter) else adapter
    assert inbound.is_complete_source() is False
    assert inbound.completeness_guaranteed() is False


def test_ocr_group_name_nfkc_exact_and_rejects_substring_missing_ambiguous(tmp_path: Path, clock: FakeClock):
    # 全半角、空白：规范化后精确匹配应发送
    driver_ok = FakeScreenDriver(
        elements=_elements(group=" 交易会员群－主群 ")
    )
    ok = _ocr_adapter(tmp_path / "ok", clock, driver_ok)
    ok.send_text("g1", "规范化精确匹配应发送")
    assert driver_ok.pastes == ["规范化精确匹配应发送"]

    # 「主群」不得匹配「交易会员群-主群」
    driver_sub = FakeScreenDriver(elements=_elements(group="主群"))
    sub = _ocr_adapter(tmp_path / "sub", clock, driver_sub)
    with pytest.raises(GroupNotConfirmedError):
        sub.send_text("g1", "子串不得发送")
    assert driver_sub.pastes == []
    assert driver_sub.clicks == []

    # OCR 缺字
    driver_miss = FakeScreenDriver(elements=_elements(group="交易会员群-主"))
    miss = _ocr_adapter(tmp_path / "miss", clock, driver_miss)
    with pytest.raises(GroupNotConfirmedError):
        miss.send_text("g1", "缺字不得发送")
    assert driver_miss.pastes == []

    # 同名多处：写 PAUSE 且不发
    pause_dir = tmp_path / "amb"
    pause_dir.mkdir()
    driver_amb = FakeScreenDriver(
        elements=_elements(
            group="交易会员群-主群",
            extra=[{"text": "交易会员群-主群", "x": 80, "y": 200, "width": 120, "height": 20, "confidence": 0.99}],
        )
    )
    amb = _ocr_adapter(pause_dir, clock, driver_amb)
    with pytest.raises(GroupNotConfirmedError, match="不唯一"):
        amb.send_text("g1", "同名多处不得发送")
    assert (pause_dir / "PAUSE").exists()
    assert driver_amb.pastes == []
    assert driver_amb.clicks == []

    # 确认规范化本身
    assert normalize_ui_text(" 交易会员群－主群 ") == normalize_ui_text("交易会员群-主群")
    assert normalize_ui_text("主群") != normalize_ui_text("交易会员群-主群")


def test_direct_send_text_hourly_limit_and_pause(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(elements=_elements())
    adapter = _ocr_adapter(
        tmp_path / "rate",
        clock,
        driver,
        limiter=None,
        enforce_rate_limit=False,
        global_replies_per_hour=40,
        dry_run=True,
    )
    for i in range(40):
        adapter.send_text("g1", f"第{i}条合规回复")
    assert len(adapter.sent_messages) == 40
    with pytest.raises(RateLimitedError, match="每小时"):
        adapter.send_text("g1", "第41条应被拦截")
    assert len(adapter.sent_messages) == 40

    pause_dir = tmp_path / "pause"
    pause_dir.mkdir()
    gate = PauseGate(data_dir=pause_dir)
    gate.pause("复测人工暂停")
    driver2 = FakeScreenDriver(elements=_elements())
    paused = _ocr_adapter(pause_dir, clock, driver2, pause=gate)
    with pytest.raises(SendPausedError):
        paused.send_text("g1", "暂停后不应发出")
    assert driver2.clicks == []
    assert driver2.pastes == []
    assert (pause_dir / "PAUSE").exists()


def test_humanized_sender_observe_does_not_send_via_ocr(tmp_path: Path, clock: FakeClock):
    """生产装配（HumanizedSender + SendGate）下 observe 不得发出。"""
    driver = FakeScreenDriver(elements=_elements())
    outbound = _ocr_adapter(tmp_path / "mode", clock, driver, dry_run=False)
    spec = BucketSpec(capacity=80, refill_per_second=100.0)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=spec,
            per_group=spec,
            announcement=spec,
            auto_reply=spec,
            global_replies_per_hour=40,
        ),
        clock=clock,
    )
    gate = SendGate(mode=RunMode.OBSERVE, store={})
    sender = HumanizedSender(outbound, limiter, clock=clock, send_gate=gate, rng=__import__("random").Random(0))
    sender.submit_auto_reply("g1", "observe 不得发出")
    sender.pump()
    assert driver.pastes == [], "HumanizedSender 在 observe 下不得调用 OCR 发送"
    assert sender.dropped_count >= 1


def test_direct_send_text_must_not_bypass_run_mode(tmp_path: Path, clock: FakeClock):
    """直发也应遵守运行模式（与 UIA 的 attach_official_sender 对等）。"""
    driver = FakeScreenDriver(elements=_elements())
    bare = _ocr_adapter(tmp_path / "bare", clock, driver)
    # 若已实现挂接，这里把 observe 闸门挂上后再直发，应被拦截。
    if hasattr(bare, "attach_official_sender"):
        spec = BucketSpec(capacity=80, refill_per_second=100.0)
        limiter = RateLimiter(
            RateLimitConfig(
                global_bucket=spec,
                per_group=spec,
                announcement=spec,
                auto_reply=spec,
                global_replies_per_hour=40,
            ),
            clock=clock,
        )
        HumanizedSender(
            bare,
            limiter,
            clock=clock,
            send_gate=SendGate(mode=RunMode.OBSERVE, store={}),
            rng=__import__("random").Random(0),
        )
        with pytest.raises((SendPausedError, RuntimeError, PermissionError, ValueError)):
            bare.send_text("g1", "直发也应被 observe 拦截")
        assert driver.pastes == []
        return
    bare.send_text("g1", "直发绕过 observe")
    assert driver.pastes == [], (
        "OcrOutboundAdapter 未实现 attach_official_sender，"
        f"直接 send_text 在无闸门时仍发出：{driver.pastes}"
    )


def test_reply_mode_allows_ocr_send_via_sender(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(elements=_elements())
    outbound = _ocr_adapter(tmp_path / "reply", clock, driver)
    spec = BucketSpec(capacity=80, refill_per_second=100.0)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=spec,
            per_group=spec,
            announcement=spec,
            auto_reply=spec,
            global_replies_per_hour=40,
        ),
        clock=clock,
    )
    gate = SendGate(mode=RunMode.REPLY, store={})
    sender = HumanizedSender(outbound, limiter, clock=clock, send_gate=gate, rng=__import__("random").Random(0))
    sender.submit_auto_reply("g1", "reply 模式应发出")
    sender.pump()
    assert driver.pastes == ["reply 模式应发出"]


def test_ungated_without_send_gate_is_rejected_by_default(tmp_path: Path, clock: FakeClock):
    """未挂正式闸门、也未注入 SendGate 时，默认拒发（等同 observe）。"""
    driver = FakeScreenDriver(elements=_elements())
    adapter = _ocr_adapter(
        tmp_path / "ungated",
        clock,
        driver,
        allow_ungated_for_tests=False,
    )
    with pytest.raises(RuntimeError, match="未挂接正式发送闸门"):
        adapter.send_text("g1", "无闸门不得直发")
    assert driver.pastes == []
    assert driver.clicks == []
    assert adapter.sent_messages == []
    assert adapter.history[-1].status == "rejected"


def test_observe_send_text_is_rejected_with_injected_gate(tmp_path: Path, clock: FakeClock):
    """注入 observe 闸门后，直接 send_text 必须被拒。"""
    driver = FakeScreenDriver(elements=_elements())
    adapter = _ocr_adapter(
        tmp_path / "observe",
        clock,
        driver,
        allow_ungated_for_tests=False,
        send_gate=SendGate(mode=RunMode.OBSERVE, store={}),
    )
    with pytest.raises((RuntimeError, PermissionError, ValueError, SendPausedError)):
        adapter.send_text("g1", "observe 下不得发出")
    assert driver.pastes == []
    assert driver.clicks == []
    assert adapter.sent_messages == []
    assert adapter.history[-1].ok is False


def test_gated_ocr_respects_run_mode_pause_and_hourly_cap(tmp_path: Path, clock: FakeClock):
    """挂接正式闸门后，应遵守运行模式、PAUSE 与每小时 40 条上限。"""
    # observe：拒发
    driver_obs = FakeScreenDriver(elements=_elements())
    outbound_obs = _ocr_adapter(
        tmp_path / "gate_obs",
        clock,
        driver_obs,
        allow_ungated_for_tests=False,
    )
    spec = BucketSpec(capacity=80, refill_per_second=100.0)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=spec,
            per_group=spec,
            announcement=spec,
            auto_reply=spec,
            global_replies_per_hour=40,
        ),
        clock=clock,
    )
    HumanizedSender(
        outbound_obs,
        limiter,
        clock=clock,
        send_gate=SendGate(mode=RunMode.OBSERVE, store={}),
        rng=__import__("random").Random(0),
    )
    with pytest.raises((RuntimeError, PermissionError, ValueError, SendPausedError)):
        outbound_obs.send_text("g1", "observe 挂闸后仍不得直发")
    assert driver_obs.pastes == []

    # reply + PAUSE：拒发
    pause_dir = tmp_path / "gate_pause"
    pause_dir.mkdir()
    gate = PauseGate(data_dir=pause_dir)
    gate.pause("复测挂闸后暂停")
    driver_pause = FakeScreenDriver(elements=_elements())
    outbound_pause = _ocr_adapter(
        pause_dir,
        clock,
        driver_pause,
        allow_ungated_for_tests=False,
        pause=gate,
    )
    HumanizedSender(
        outbound_pause,
        limiter,
        clock=clock,
        send_gate=SendGate(mode=RunMode.REPLY, pause_gate=gate, store={}),
        rng=__import__("random").Random(0),
    )
    with pytest.raises(SendPausedError):
        outbound_pause.send_text("g1", "暂停后不应发出")
    assert driver_pause.pastes == []
    assert driver_pause.clicks == []
    assert (pause_dir / "PAUSE").exists()

    # reply 下直发遵守 40/小时（适配器内置上限，dry_run 避免真实键鼠）
    driver_rate = FakeScreenDriver(elements=_elements())
    outbound_rate = _ocr_adapter(
        tmp_path / "gate_rate",
        clock,
        driver_rate,
        allow_ungated_for_tests=False,
        global_replies_per_hour=40,
        dry_run=True,
        send_gate=SendGate(mode=RunMode.REPLY, store={}),
    )
    for i in range(40):
        outbound_rate.send_text("g1", f"挂闸后第{i}条")
    assert len(outbound_rate.sent_messages) == 40
    with pytest.raises(RateLimitedError, match="每小时"):
        outbound_rate.send_text("g1", "挂闸后第41条应被拦截")
    assert len(outbound_rate.sent_messages) == 40
    assert outbound_rate.history[-1].status == "rate_limited"

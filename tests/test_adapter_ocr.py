"""D 方案：OCR 降级发送的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.mock import MockAdapter
from src.adapters.ocr import (
    CaptchaOrDialogError,
    DEFAULT_OCR_THRESHOLD,
    FakeOcrEngine,
    FakeScreenDriver,
    GroupNotConfirmedError,
    OcrFallback,
    OcrOutboundAdapter,
    RateLimitedError,
    SendIdempotencyLog,
    TextBox,
    WindowsScreenDriver,
)
from src.adapters.pause import PauseGate, SendPausedError
from src.adapters.roles import IMPLEMENTED_OUTBOUND, OUTBOUND_OCR
from src.clock import FakeClock
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizedSender
from src.run_mode import RunMode, SendGate


def _limiter(clock: FakeClock, *, capacity: float = 8, refill: float = 10.0) -> RateLimiter:
    spec = BucketSpec(capacity=capacity, refill_per_second=refill)
    cfg = RateLimitConfig(
        global_bucket=spec,
        per_group=spec,
        announcement=spec,
        auto_reply=spec,
        global_replies_per_hour=40,
    )
    return RateLimiter(cfg, clock=clock)


def _ready_driver(group: str = "交易会员群-主群") -> FakeScreenDriver:
    return FakeScreenDriver(
        elements=[
            {"text": group, "x": 80, "y": 20, "width": 120, "height": 20, "confidence": 0.99},
            {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": 0.99},
            {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": 0.99},
        ]
    )


def test_module_imports_on_linux():
    assert "src.adapters.ocr" in sys.modules


def test_windows_driver_refuses_linux():
    with pytest.raises(RuntimeError, match="只能在 Windows 上使用"):
        WindowsScreenDriver()


def test_ocr_fallback_high_confidence_not_degraded():
    ocr = OcrFallback(
        scripted=[{"text": "请问开户要哪些资料", "confidence": 0.96}],
        threshold=DEFAULT_OCR_THRESHOLD,
    )
    result = ocr.recognize(image=b"fake-image")
    assert result.confidence >= DEFAULT_OCR_THRESHOLD
    assert result.fallback_used is False
    assert ocr.should_fallback(result) is False


def test_ocr_fallback_low_confidence_and_empty_and_error():
    ocr = OcrFallback(
        scripted=[
            {"text": "模煳文字", "confidence": 0.21},
            {"text": "", "confidence": 0.99},
            {"text": "x", "confidence": 0.0, "error": "engine_failed"},
        ],
        threshold=DEFAULT_OCR_THRESHOLD,
    )
    low = ocr.recognize(image=b"a")
    assert low.confidence < DEFAULT_OCR_THRESHOLD
    assert ocr.should_fallback(low) is True
    assert low.fallback_used is True
    assert "ocr_degraded" in low.flags

    empty = ocr.recognize(image=b"blank")
    assert ocr.should_fallback(empty) is True
    assert empty.fallback_used is True

    broken = ocr.recognize(image=b"broken")
    assert ocr.should_fallback(broken) is True
    assert broken.fallback_used is True
    assert "ocr_degraded" in broken.flags


def test_send_clicks_and_pastes(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        engine=FakeOcrEngine(default=__import__("src.adapters.ocr", fromlist=["OcrResult"]).OcrResult(text="交易会员群-主群 输入 发送", confidence=0.99)),
        limiter=_limiter(clock),
        data_dir=tmp_path,
        clock=clock,
        rng=__import__("random").Random(1),
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.4,
        input_pause_max=0.4,
    )
    adapter.send_text("g1", "测试发送（虚构，请忽略）")
    assert driver.pastes == ["测试发送（虚构，请忽略）"]
    assert len(driver.clicks) == 2
    assert driver.clicks[0] == (240, 412)  # 输入框中心 200+40, 400+12
    assert driver.clicks[1] == (384, 412)
    assert adapter.sent_messages[0].text == "测试发送（虚构，请忽略）"
    assert adapter.history[-1].status == "ok"
    # 输入停顿被假时钟记录
    assert any(s >= 0.4 for s in clock.sleeps)


def test_dry_run_does_not_click(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=_limiter(clock),
        data_dir=tmp_path,
        clock=clock,
        dry_run=True,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    adapter.send_text("g1", "不会真正发送")
    assert driver.clicks == []
    assert driver.pastes == []
    assert adapter.history[-1].status == "dry_run"
    assert adapter.sent_messages[0].extra.get("dry_run") is True


def test_pause_file_blocks_send(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    gate = PauseGate(data_dir=tmp_path)
    gate.pause("人工暂停")
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=_limiter(clock),
        pause=gate,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    with pytest.raises(SendPausedError, match="紧急暂停"):
        adapter.send_text("g1", "不应发出")
    assert driver.clicks == []
    assert driver.pastes == []


def test_captcha_creates_pause_and_stops(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(
        elements=[
            {"text": "交易会员群-主群", "x": 10, "y": 10, "confidence": 0.99},
            {"text": "请输入验证码", "x": 80, "y": 80, "confidence": 0.99},
            {"text": "输入", "x": 20, "y": 400, "confidence": 0.99},
            {"text": "发送", "x": 80, "y": 400, "confidence": 0.99},
        ],
        dialogs=["验证码"],
    )
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        engine=FakeOcrEngine(scripted=[{"text": "请输入验证码", "confidence": 0.99}]),
        limiter=_limiter(clock),
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    with pytest.raises(CaptchaOrDialogError, match="验证码"):
        adapter.send_text("g1", "不应发出")
    assert (tmp_path / "PAUSE").exists()
    assert driver.clicks == []
    assert driver.pastes == []


def test_idempotent_does_not_resend(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=_limiter(clock),
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    adapter.send_text("g1", "同一句回复")
    clicks_after_first = list(driver.clicks)
    adapter.send_text("g1", "同一句回复")
    assert driver.clicks == clicks_after_first
    assert adapter.history[-1].status == "skipped_duplicate"
    assert len(adapter.sent_messages) == 1


def test_idempotency_survives_rebuild(tmp_path: Path, clock: FakeClock):
    log = SendIdempotencyLog(tmp_path / "ocr_idempotency.json", window_seconds=600, clock=clock)
    log.record("g1", "hello")
    again = SendIdempotencyLog(tmp_path / "ocr_idempotency.json", window_seconds=600, clock=clock)
    assert again.already_sent("g1", "hello") is True
    assert again.already_sent("g1", "other") is False


def test_rate_limiter_is_enforced(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    limiter = _limiter(clock, capacity=1, refill=0.0)
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=limiter,
        enforce_rate_limit=True,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
        max_rate_wait_seconds=1.0,
    )
    adapter.send_text("g1", "第一条")
    with pytest.raises(RateLimitedError):
        adapter.send_text("g1", "第二条应被限速")
    assert len(adapter.sent_messages) == 1


def test_group_not_confirmed_aborts(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(
        elements=[
            {"text": "输入", "x": 20, "y": 400, "confidence": 0.99},
            {"text": "发送", "x": 80, "y": 400, "confidence": 0.99},
        ]
    )
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=_limiter(clock),
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    with pytest.raises(GroupNotConfirmedError, match="确认群名"):
        adapter.send_text("g1", "不能发错群")
    assert driver.pastes == []


def test_role_is_implemented_and_factory_builds_it(tmp_path: Path, clock: FakeClock, monkeypatch):
    assert OUTBOUND_OCR in IMPLEMENTED_OUTBOUND
    monkeypatch.chdir(tmp_path)
    adapter = build_adapter(
        {
            "wechat": {
                "inbound": "mock",
                "outbound": "ocr",
                "ocr": {"dry_run": True, "data_dir": str(tmp_path)},
            }
        },
        clock=clock,
    )
    from src.adapters.composite import CompositeAdapter

    assert isinstance(adapter, CompositeAdapter)
    assert isinstance(adapter.outbound, OcrOutboundAdapter)

def test_group_name_substring_is_not_confirmation(tmp_path: Path, clock: FakeClock):
    from src.adapters.ocr import normalize_ui_text

    assert normalize_ui_text(" 交易会员群－主群 ") == normalize_ui_text("交易会员群-主群")
    driver = _ready_driver()
    # 覆盖为截断子串
    driver.elements = [
        __import__("src.adapters.ocr", fromlist=["TextBox"]).TextBox(text="主群", x=80, y=20, width=40, height=20, confidence=0.99),
        __import__("src.adapters.ocr", fromlist=["TextBox"]).TextBox(text="输入", x=200, y=400, width=80, height=24, confidence=0.99),
        __import__("src.adapters.ocr", fromlist=["TextBox"]).TextBox(text="发送", x=360, y=400, width=48, height=24, confidence=0.99),
    ]
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    with pytest.raises(GroupNotConfirmedError, match="确认群名"):
        adapter.send_text("g1", "不能凭子串发送")
    assert driver.clicks == []
    assert driver.pastes == []


def test_normalized_exact_group_name_still_sends(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(
        elements=[
            {"text": " 交易会员群－主群 ", "x": 80, "y": 20, "width": 120, "height": 20, "confidence": 0.99},
            {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": 0.99},
            {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": 0.99},
        ]
    )
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    adapter.send_text("g1", "规范化后精确匹配应发送")
    assert driver.pastes == ["规范化后精确匹配应发送"]


def test_ambiguous_group_name_pauses_and_does_not_send(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(
        elements=[
            {"text": "交易会员群-主群", "x": 80, "y": 20, "width": 120, "height": 20, "confidence": 0.99},
            {"text": "交易会员群-主群", "x": 80, "y": 200, "width": 120, "height": 20, "confidence": 0.99},
            {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": 0.99},
            {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": 0.99},
        ]
    )
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    with pytest.raises(GroupNotConfirmedError, match="不唯一"):
        adapter.send_text("g1", "匹配不唯一不得发送")
    assert (tmp_path / "PAUSE").exists()
    assert driver.clicks == []
    assert driver.pastes == []


def test_direct_send_text_enforces_hourly_limit_without_limiter(tmp_path: Path, clock: FakeClock):
    """enforce_rate_limit=false 且未注入限速器时，直接 send_text 仍受每小时 40 条上限约束。"""
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        global_replies_per_hour=40,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
        dry_run=True,
    )
    for i in range(40):
        adapter.send_text("g1", f"第{i}条合规回复")
    assert len(adapter.sent_messages) == 40
    with pytest.raises(RateLimitedError, match="每小时"):
        adapter.send_text("g1", "第41条应被每小时上限拦截")
    assert len(adapter.sent_messages) == 40
    assert adapter.history[-1].status == "rate_limited"


def test_attach_official_sender_observe_blocks_direct_send(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    assert hasattr(adapter, "attach_official_sender")
    HumanizedSender(
        adapter,
        _limiter(clock),
        clock=clock,
        send_gate=SendGate(mode=RunMode.OBSERVE, store={}),
        rng=__import__("random").Random(0),
    )
    with pytest.raises((SendPausedError, RuntimeError, PermissionError, ValueError), match="observe|不允许"):
        adapter.send_text("g1", "直发也应被 observe 拦截")
    assert driver.pastes == []
    assert adapter.last_result is not None
    assert adapter.last_result.ok is False


def test_ungated_send_text_is_rejected_by_default(tmp_path: Path, clock: FakeClock, caplog):
    """未挂接正式出口、也未注入 SendGate 时默认拒发，且日志不含正文。"""
    import logging

    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    secret = "直发绕过 observe 的正文不应出现在日志"
    with caplog.at_level(logging.INFO, logger="wechat_bot"):
        with pytest.raises(RuntimeError, match="未挂接正式发送闸门"):
            adapter.send_text("g1", secret)
    assert driver.pastes == []
    assert driver.clicks == []
    assert adapter.last_result is not None
    assert adapter.last_result.status == "rejected"
    assert secret not in caplog.text


def test_allow_ungated_for_tests_still_sends(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        allow_ungated_for_tests=True,
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    adapter.send_text("g1", "测试开关下放行")
    assert driver.pastes == ["测试开关下放行"]


def test_injected_send_gate_reply_allows_direct_send(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    adapter = OcrOutboundAdapter(
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
        send_gate=SendGate(mode=RunMode.REPLY, store={}),
    )
    adapter.send_text("g1", "注入 reply 闸门应发出")
    assert driver.pastes == ["注入 reply 闸门应发出"]


def test_official_path_does_not_double_charge_tokens(tmp_path: Path, clock: FakeClock):
    """HumanizedSender 已扣令牌后，through_official 不得再扣同一限速器。"""
    driver = _ready_driver()
    limiter = _limiter(clock, capacity=2, refill=0.0)
    adapter = OcrOutboundAdapter(
        driver=driver,
        limiter=limiter,
        enforce_rate_limit=True,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
        global_replies_per_hour=40,
    )
    sender = HumanizedSender(
        adapter,
        limiter,
        clock=clock,
        send_gate=SendGate(mode=RunMode.REPLY, store={}),
        rng=__import__("random").Random(0),
    )
    sender.submit_auto_reply("g1", "官方出口第一条")
    sender.pump()
    sender.submit_auto_reply("g1", "官方出口第二条")
    sender.pump()
    assert driver.pastes == ["官方出口第一条", "官方出口第二条"], (
        "若二次扣令牌，容量为 2 时第二条会被吞掉"
    )
    sender.submit_auto_reply("g1", "官方出口第三条应被限速")
    sender.pump()
    assert driver.pastes == ["官方出口第一条", "官方出口第二条"]


def test_official_path_still_enforces_internal_hourly_cap(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    # 官方限速器给宽松配额，适配器内置上限仍为 2，避免重复扣令牌的同时保住小时上限。
    spec = BucketSpec(capacity=80, refill_per_second=100.0)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=spec,
            per_group=spec,
            announcement=spec,
            auto_reply=spec,
            global_replies_per_hour=80,
        ),
        clock=clock,
    )
    adapter = OcrOutboundAdapter(
        driver=driver,
        limiter=limiter,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
        global_replies_per_hour=2,
        dry_run=True,
    )
    sender = HumanizedSender(
        adapter,
        limiter,
        clock=clock,
        send_gate=SendGate(mode=RunMode.REPLY, store={}),
        rng=__import__("random").Random(0),
    )
    sender.submit_auto_reply("g1", "小时上限第1条")
    sender.pump()
    sender.submit_auto_reply("g1", "小时上限第2条")
    sender.pump()
    assert len(adapter.sent_messages) == 2
    sender.submit_auto_reply("g1", "小时上限第3条")
    sender.pump()
    assert len(adapter.sent_messages) == 2
    assert adapter.last_result is not None
    assert adapter.last_result.status == "rate_limited"


def test_composite_forwards_official_sender_to_ocr(tmp_path: Path, clock: FakeClock):
    driver = _ready_driver()
    outbound = OcrOutboundAdapter(
        driver=driver,
        limiter=None,
        enforce_rate_limit=False,
        data_dir=tmp_path,
        clock=clock,
        group_names={"g1": "交易会员群-主群"},
        input_pause_min=0.0,
        input_pause_max=0.0,
    )
    inbound = MockAdapter(groups=[], clock=clock)
    composite = CompositeAdapter(inbound=inbound, outbound=outbound)
    HumanizedSender(
        composite,
        _limiter(clock),
        clock=clock,
        send_gate=SendGate(mode=RunMode.OBSERVE, store={}),
        rng=__import__("random").Random(0),
    )
    with pytest.raises((SendPausedError, RuntimeError, PermissionError, ValueError)):
        composite.send_text("g1", "组合层直发也应被拦截")
    assert driver.pastes == []

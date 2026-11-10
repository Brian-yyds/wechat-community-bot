# -*- coding: utf-8 -*-
"""D 方案补充用例：低置信度、错群、PAUSE、每小时上限、验证码检测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.adapters.ocr import (
    DEFAULT_OCR_THRESHOLD,
    CaptchaOrDialogError,
    FakeOcrEngine,
    FakeScreenDriver,
    GroupNotConfirmedError,
    OcrOutboundAdapter,
    RateLimitedError,
)
from src.adapters.pause import PauseGate, SendPausedError
from src.clock import FakeClock
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter


def _limiter(clock: FakeClock, *, capacity: float = 80, refill: float = 100.0, per_hour: int = 40) -> RateLimiter:
    spec = BucketSpec(capacity=capacity, refill_per_second=refill)
    cfg = RateLimitConfig(
        global_bucket=spec,
        per_group=spec,
        announcement=spec,
        auto_reply=spec,
        global_replies_per_hour=per_hour,
    )
    return RateLimiter(cfg, clock=clock)


def _elements(group: str = "交易会员群-主群", confidence: float = 0.99) -> list[dict]:
    return [
        {"text": group, "x": 80, "y": 20, "width": 120, "height": 20, "confidence": confidence},
        {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": confidence},
        {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": confidence},
    ]


def _adapter(tmp_path: Path, clock: FakeClock, driver: FakeScreenDriver, **kwargs) -> OcrOutboundAdapter:
    kwargs.setdefault("limiter", _limiter(clock))
    kwargs.setdefault("enforce_rate_limit", True)
    kwargs.setdefault("data_dir", tmp_path)
    kwargs.setdefault("clock", clock)
    kwargs.setdefault("group_names", {"g1": "交易会员群-主群"})
    kwargs.setdefault("input_pause_min", 0.0)
    kwargs.setdefault("input_pause_max", 0.0)
    kwargs.setdefault("pause", PauseGate(data_dir=tmp_path))
    # 机械用例（群名匹配、验证码、截图等）需要直发；生产默认无闸门拒发由 review_cd 新增用例覆盖。
    kwargs.setdefault("allow_ungated_for_tests", True)
    return OcrOutboundAdapter(driver=driver, **kwargs)


def test_low_confidence_does_not_send(tmp_path: Path, clock: FakeClock):
    low = DEFAULT_OCR_THRESHOLD - 0.2
    assert low < DEFAULT_OCR_THRESHOLD
    driver = FakeScreenDriver(elements=_elements(confidence=low))
    adapter = _adapter(
        tmp_path,
        clock,
        driver,
        engine=FakeOcrEngine(scripted=[{"text": "交易会员群-主群 输入 发送", "confidence": low}]),
    )
    with pytest.raises(GroupNotConfirmedError):
        adapter.send_text("g1", "低置信度不应发送")
    assert driver.clicks == []
    assert driver.pastes == []
    assert adapter.history[-1].status == "group_not_confirmed"
    assert adapter.sent_messages == []


def test_ocr_misread_group_substring_must_not_send_to_wrong_chat(tmp_path: Path, clock: FakeClock):
    """屏幕上只有群名的截断/子串（如「主群」）时，不得把目标群「交易会员群-主群」视为已确认。"""
    driver = FakeScreenDriver(
        elements=[
            {"text": "主群", "x": 80, "y": 20, "width": 40, "height": 20, "confidence": 0.99},
            {"text": "输入", "x": 200, "y": 400, "width": 80, "height": 24, "confidence": 0.99},
            {"text": "发送", "x": 360, "y": 400, "width": 48, "height": 24, "confidence": 0.99},
        ]
    )
    adapter = _adapter(tmp_path, clock, driver)
    with pytest.raises(GroupNotConfirmedError, match="确认群名"):
        adapter.send_text("g1", "不能凭子串发错群")
    assert driver.clicks == []
    assert driver.pastes == []


def test_wrong_group_name_on_screen_does_not_send(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(elements=_elements(group="交易会员群-备群", confidence=0.99))
    adapter = _adapter(tmp_path, clock, driver)
    with pytest.raises(GroupNotConfirmedError, match="确认群名"):
        adapter.send_text("g1", "不能发到备群")
    assert driver.pastes == []


def test_pause_file_rejects_send(tmp_path: Path, clock: FakeClock):
    gate = PauseGate(data_dir=tmp_path)
    gate.pause("核查人工暂停")
    driver = FakeScreenDriver(elements=_elements())
    adapter = _adapter(tmp_path, clock, driver, pause=gate)
    with pytest.raises(SendPausedError, match="紧急暂停"):
        adapter.send_text("g1", "暂停后不应发出")
    assert driver.clicks == []
    assert driver.pastes == []
    assert (tmp_path / "PAUSE").exists()


def test_hourly_limit_default_40_is_enforced(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(elements=_elements())
    adapter = _adapter(
        tmp_path,
        clock,
        driver,
        limiter=_limiter(clock, capacity=80, refill=100.0, per_hour=40),
        enforce_rate_limit=True,
        max_rate_wait_seconds=1.0,
    )
    for i in range(40):
        adapter.send_text("g1", f"第{i}条合规回复")
    assert len(adapter.sent_messages) == 40
    with pytest.raises(RateLimitedError):
        adapter.send_text("g1", "第41条应被每小时上限拦截")
    assert len(adapter.sent_messages) == 40
    assert adapter.history[-1].status == "rate_limited"


def test_captcha_dialog_pauses_without_solving(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(
        elements=[
            {"text": "交易会员群-主群", "x": 10, "y": 10, "confidence": 0.99},
            {"text": "请输入验证码", "x": 80, "y": 80, "confidence": 0.99},
            {"text": "输入", "x": 20, "y": 400, "confidence": 0.99},
            {"text": "发送", "x": 80, "y": 400, "confidence": 0.99},
        ],
        dialogs=["安全验证"],
    )
    adapter = _adapter(
        tmp_path,
        clock,
        driver,
        engine=FakeOcrEngine(scripted=[{"text": "请输入验证码 安全验证", "confidence": 0.99}]),
    )
    with pytest.raises(CaptchaOrDialogError, match="验证码"):
        adapter.send_text("g1", "遇到验证码不得自动处理")
    assert (tmp_path / "PAUSE").exists()
    assert driver.clicks == []
    assert driver.pastes == []
    # 不得把验证码当发送文案或尝试填写。
    reason = (tmp_path / "PAUSE").read_text(encoding="utf-8")
    assert "验证码" in reason or "安全验证" in reason


@pytest.mark.parametrize(
    "label",
    ["验证码", "安全验证", "异常登录", "环境异常", "请完成验证", "滑块", "账号异常"],
)
def test_known_risk_labels_trigger_pause(tmp_path: Path, clock: FakeClock, label: str):
    driver = FakeScreenDriver(
        elements=[{"text": label, "x": 10, "y": 10, "confidence": 0.99}],
        dialogs=[label],
    )
    adapter = _adapter(
        tmp_path,
        clock,
        driver,
        engine=FakeOcrEngine(scripted=[{"text": label, "confidence": 0.99}]),
        confirm_group_name=False,
    )
    with pytest.raises(CaptchaOrDialogError):
        adapter.send_text("g1", "风控界面不得继续点")
    assert (tmp_path / "PAUSE").exists()
    assert driver.clicks == []
    assert driver.pastes == []


def test_dry_run_writes_no_screenshot_files(tmp_path: Path, clock: FakeClock):
    driver = FakeScreenDriver(elements=_elements(), image=b"fake-screenshot")
    adapter = _adapter(tmp_path, clock, driver, dry_run=True)
    adapter.send_text("g1", "dry-run 不应落盘截图")
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}]
    assert leftovers == []
    assert adapter.history[-1].status == "dry_run"
    assert driver.clicks == []

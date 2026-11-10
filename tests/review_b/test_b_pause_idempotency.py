# -*- coding: utf-8 -*-
"""PAUSE 隔离、异常即暂停、幂等与重启持久化。"""
from __future__ import annotations

from pathlib import Path

from src.adapters.ui_driver import FakeUiDriver, UiDriver
from src.adapters.uia import SentIdStore, UiaOutboundAdapter, UiaSender, UiaSendStatus
from src.clock import FakeClock

from review_b.helpers import make_adapter, make_config

REPO = Path(__file__).resolve().parents[2]


def test_existing_pause_file_rejects_send_without_ui_ops(tmp_path: Path):
    flag = tmp_path / "PAUSE"
    flag.write_text("manual stop", encoding="utf-8")
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "暂停后不应发出")
    assert not result.ok
    assert result.status == UiaSendStatus.PAUSED
    assert driver.opened == []
    assert driver.pasted == []
    assert driver.pressed == []


def test_clear_emergency_pause_only_touches_isolated_flag(tmp_path: Path):
    """clear_emergency_pause / resume 只能删除本适配器的暂停文件，不能动仓库 data/PAUSE。"""
    repo_pause = REPO / "data" / "PAUSE"
    existed = repo_pause.exists()
    adapter = make_adapter(tmp_path)
    (tmp_path / "PAUSE").write_text("x", encoding="utf-8")
    adapter.clear_emergency_pause()
    assert not (tmp_path / "PAUSE").exists()
    if existed:
        assert repo_pause.exists()
    else:
        assert not repo_pause.exists()


def test_contract_uia_sender_default_does_not_touch_repo_pause():
    repo_pause = REPO / "data" / "PAUSE"
    existed = repo_pause.exists()
    sender = UiaSender(fail_mode="ui_not_ready")
    result = sender.send("交易会员群-主群", "隔离探测")
    assert not result.ok
    assert result.status == UiaSendStatus.UI_NOT_READY
    flag = sender.adapter.config.resolve_pause_flag(sender.adapter.project_root)
    assert flag.exists()
    assert REPO / "data" / "PAUSE" != flag
    assert REPO / "data" not in flag.parents
    if not existed:
        assert not repo_pause.exists()


def test_lock_logout_dialog_immediately_write_pause_and_never_relogin(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)

    cases = [
        ("locked", "locked", "锁屏"),
        ("logged_in", "logged_out", "掉线或被踢"),
        ("window_visible", "window_not_visible", "窗口不可见"),
        ("blocking_dialog", "blocking_dialog", "验证码或异常弹窗"),
    ]
    # 锁屏
    driver.locked = True
    r = adapter.send("g1", "锁屏")
    assert r.status == UiaSendStatus.UI_NOT_READY
    assert adapter.emergency_paused
    assert (tmp_path / "PAUSE").exists()
    reason = (tmp_path / "PAUSE").read_text(encoding="utf-8")
    assert "locked" in reason or "锁屏" in reason
    assert driver.login_attempts == 0
    assert driver.pressed == []
    # 解锁后不得自动恢复
    driver.locked = False
    r2 = adapter.send("g1", "仍应暂停")
    assert r2.status == UiaSendStatus.PAUSED
    adapter.clear_emergency_pause()

    driver.logged_in = False
    r = adapter.send("g1", "掉线")
    assert r.status == UiaSendStatus.UI_NOT_READY
    assert "自动重登" in (r.error or adapter.pause_reason) or "掉线" in (r.error or "")
    assert driver.login_attempts == 0
    alerts = {a["code"] for a in adapter.alerts}
    assert "logged_out" in alerts
    adapter.clear_emergency_pause()
    driver.logged_in = True

    driver.window_visible = False
    r = adapter.send("g1", "不可见")
    assert r.status == UiaSendStatus.UI_NOT_READY
    adapter.clear_emergency_pause()
    driver.window_visible = True

    driver.blocking_dialog = True
    r = adapter.send("g1", "验证码")
    assert r.status == UiaSendStatus.UI_NOT_READY
    assert any(a["code"] == "blocking_dialog" for a in adapter.alerts)
    assert "不自动处理" in (r.error or adapter.pause_reason)
    assert driver.login_attempts == 0
    assert driver.pressed == []
    assert not hasattr(driver, "solve_captcha") or getattr(driver, "captcha_attempts", 0) == 0


def test_pyweixin_driver_has_no_auto_login_or_captcha_solver():
    from src.adapters.pyweixin_driver import PyWeixinDriver

    forbidden = {
        "attempt_login",
        "login",
        "relogin",
        "auto_login",
        "solve_captcha",
        "bypass_captcha",
        "enter_captcha",
        "scan_qr",
        "confirm_login",
    }
    methods = {n for n in dir(PyWeixinDriver) if not n.startswith("_")}
    overlap = methods & forbidden
    assert overlap == set(), f"真实驱动出现自动重登或打码方法: {overlap}"
    # UiDriver 接口也不应要求实现自动登录
    abstract = {n for n in dir(UiDriver) if not n.startswith("_")}
    assert not (abstract & {"attempt_login", "solve_captcha", "relogin"})


def test_idempotent_same_send_id_only_presses_once(tmp_path: Path):
    adapter = make_adapter(tmp_path)
    first = adapter.send("g1", "同一条", send_id="msg-1")
    second = adapter.send("g1", "同一条", send_id="msg-1")
    assert first.ok and not first.skipped
    assert second.ok and second.skipped
    assert second.error == "idempotent_skip"
    assert adapter.driver.pressed.count("交易会员群-主群") == 1  # type: ignore[attr-defined]


def test_maybe_sent_does_not_resend_but_first_status_should_be_maybe_sent(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_mode="rejected")
    adapter = make_adapter(tmp_path, driver=driver)
    first = adapter.send("g1", "结果不明", send_id="msg-maybe")
    assert not first.ok
    assert len(driver.pressed) == 1
    # 验收：按下后校验不确定应记 maybe_sent，而不是容易被误读的 rejected
    assert first.status == UiaSendStatus.MAYBE_SENT, (
        f"按下发送后校验失败的首次状态应为 maybe_sent，实际为 {first.status}"
    )
    again = adapter.send("g1", "结果不明", send_id="msg-maybe")
    assert again.skipped
    assert again.status == UiaSendStatus.MAYBE_SENT
    assert len(driver.pressed) == 1


def test_maybe_sent_and_sent_survive_restart(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    ok = adapter.send("g1", "已发出", send_id="persist-ok")
    assert ok.ok
    driver2 = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_mode="rejected")
    adapter_m = make_adapter(tmp_path, driver=driver2)
    maybe = adapter_m.send("g1", "可能已发出", send_id="persist-maybe")
    assert not maybe.ok
    store_path = tmp_path / "data" / "uia_sent_ids.json"
    assert store_path.exists()

    # 新进程/新适配器实例应读到同一份记录
    restarted = make_adapter(tmp_path, driver=FakeUiDriver(known_chats={"交易会员群-主群"}))
    skip_ok = restarted.send("g1", "已发出", send_id="persist-ok")
    skip_maybe = restarted.send("g1", "可能已发出", send_id="persist-maybe")
    assert skip_ok.skipped and skip_ok.ok
    assert skip_maybe.skipped and skip_maybe.status == UiaSendStatus.MAYBE_SENT
    assert restarted.driver.pressed == []  # type: ignore[attr-defined]


def test_corrupt_sent_id_store_must_not_silently_allow_resend(tmp_path: Path):
    """幂等文件损坏时，当前实现会清空记录并以空记录继续，重启后可能重发。"""
    adapter = make_adapter(tmp_path)
    assert adapter.send("g1", "先发出", send_id="fragile").ok
    store_path = tmp_path / "data" / "uia_sent_ids.json"
    store_path.write_text("{not-json", encoding="utf-8")
    restarted = make_adapter(tmp_path)
    again = restarted.send("g1", "先发出", send_id="fragile")
    assert again.skipped or not again.ok, (
        "uia_sent_ids.json 损坏后仍成功重发同一 send_id；应失败关闭或拒绝自动重发，改由人工对账"
    )
    assert restarted.driver.pressed == []  # type: ignore[attr-defined]


def test_timeout_after_press_is_maybe_sent_and_not_retried(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_on_call={"verify_sent": 1}, fail_mode="timeout")
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "按下后超时", send_id="msg-timeout")
    assert not result.ok
    assert result.status == UiaSendStatus.MAYBE_SENT
    assert len(driver.pressed) == 1
    again = adapter.send("g1", "按下后超时", send_id="msg-timeout")
    assert again.skipped
    assert len(driver.pressed) == 1


def test_sent_id_store_flush_is_atomic_like(tmp_path: Path):
    path = tmp_path / "store.json"
    store = SentIdStore(path)
    store.mark("a", "sent")
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert store.get("a") == "sent"
    reloaded = SentIdStore(path)
    assert reloaded.get("a") == "sent"


def test_unexpected_safety_error_must_not_crash_or_send(tmp_path: Path):
    """安全探活抛出非 UiaError 时，当前实现会让 send() 直接异常外溢，既不暂停也不返回结果。"""

    class BoomDriver(FakeUiDriver):
        def is_locked(self) -> bool:
            raise RuntimeError("意外探活失败")

    driver = BoomDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "探活异常")
    assert result is not None
    assert not result.ok
    assert result.status in {UiaSendStatus.UI_NOT_READY, UiaSendStatus.PAUSED}
    assert (tmp_path / "PAUSE").exists()
    assert driver.pressed == []

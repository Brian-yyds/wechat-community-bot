"""适配器 B（UIA 发送）单元测试：全部走 FakeUiDriver，不触达真实微信。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from src.adapters.composite import CompositeAdapter
from src.adapters.factory import build_adapter
from src.adapters.mock import MockAdapter
from src.adapters.roles import IMPLEMENTED_OUTBOUND, OUTBOUND_UIA, PENDING_SLOTS
from src.adapters.ui_driver import FakeUiDriver, UiaDependencyError, normalize_chat_title, titles_match
from src.adapters.uia import (
    UiaConfig,
    UiaOutboundAdapter,
    UiaSender,
    UiaSendStatus,
)
from src.clock import FakeClock
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig

# 所有本文件用例都把紧急暂停文件指到临时目录，避免写入仓库 data/PAUSE。
@pytest.fixture(autouse=True)
def _isolate_repo_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_BOT_PAUSE_FILE", str(tmp_path / "REPO_ISOLATED_PAUSE"))
    monkeypatch.setenv("WECHAT_BOT_DATA_DIR", str(tmp_path / "data"))



def _cfg(tmp_path: Path, **kwargs) -> UiaConfig:
    data = dict(
        data_dir=str(tmp_path),
        pause_flag=str(tmp_path / "PAUSE"),
        input_pause_min=0.0,
        input_pause_max=0.0,
        group_switch_interval_min=0.0,
        group_switch_interval_max=0.0,
        min_send_interval_seconds=0.0,
        preheat_on_start=False,
        keep_chat_open=False,
        max_retries=2,
        send_timeout_seconds=8.0,
        group_names={"g1": "交易会员群-主群"},
    )
    data.update(kwargs)
    return UiaConfig(**data)


def _adapter(tmp_path: Path, driver: FakeUiDriver | None = None, clock=None, **kwargs) -> UiaOutboundAdapter:
    return UiaOutboundAdapter(
        driver=driver or FakeUiDriver(known_chats={"交易会员群-主群", "文件传输助手"}),
        config=_cfg(tmp_path, **kwargs),
        clock=clock or FakeClock(),
        project_root=tmp_path,
    )


def test_send_success_returns_ok_and_timings(tmp_path: Path):
    adapter = _adapter(tmp_path)
    result = adapter.send("g1", "测试发送（虚构，请忽略）")
    assert result.ok
    assert result.status == UiaSendStatus.OK
    assert result.timings.adapter_total >= 0
    for name in ("open_chat", "paste", "send", "verify"):
        assert hasattr(result.timings, name)
        assert getattr(result.timings, name) >= 0
    assert adapter.driver.pressed  # type: ignore[attr-defined]
    assert adapter.sent_messages[0].text == "测试发送（虚构，请忽略）"


def test_contract_sender_factory_success_and_fail_modes():
    sender = UiaSender()
    result = sender.send("交易会员群-主群", "测试发送（虚构，请忽略）")
    assert result.ok
    assert result.status.lower() in {"ok", "success", "sent"}

    modes = ["timeout", "target_not_found", "ui_not_ready"]
    statuses = {}
    for mode in modes:
        sender = UiaSender(fail_mode=mode)
        t0 = time.perf_counter()
        result = sender.send("不存在的群", "ping", timeout=0.2)
        elapsed = time.perf_counter() - t0
        assert not result.ok, f"{mode} 不应成功: {result}"
        statuses[mode] = result.status
        assert elapsed < 2.0
    assert len(set(statuses.values())) == len(modes)
    assert "timeout" in statuses["timeout"].lower() or "超时" in statuses["timeout"]
    assert "not_found" in statuses["target_not_found"].lower() or "找不到" in statuses["target_not_found"]
    assert "ui" in statuses["ui_not_ready"].lower() or "窗口" in statuses["ui_not_ready"] or "not_ready" in statuses["ui_not_ready"].lower()


def test_timeout_path_does_not_block(tmp_path: Path):
    driver = FakeUiDriver(fail_mode="timeout")
    adapter = _adapter(tmp_path, driver=driver)
    t0 = time.perf_counter()
    result = adapter.send("g1", "timeout-probe", timeout=0.3)
    elapsed = time.perf_counter() - t0
    assert not result.ok
    assert result.status == UiaSendStatus.TIMEOUT
    assert elapsed < 2.0


def test_idempotent_same_send_id_only_once(tmp_path: Path):
    adapter = _adapter(tmp_path)
    first = adapter.send("g1", "同一条", send_id="msg-1")
    second = adapter.send("g1", "同一条", send_id="msg-1")
    assert first.ok and not first.skipped
    assert second.ok and second.skipped
    assert second.error == "idempotent_skip"
    assert adapter.driver.pressed.count("交易会员群-主群") == 1  # type: ignore[attr-defined]


def test_no_retry_after_press_send(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_mode="rejected")
    adapter = _adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "可能已发出", send_id="msg-maybe")
    assert not result.ok
    assert result.status == UiaSendStatus.MAYBE_SENT
    assert len(driver.pressed) == 1
    # 再次用同一 id 不得重发
    again = adapter.send("g1", "可能已发出", send_id="msg-maybe")
    assert again.skipped
    assert len(driver.pressed) == 1


def test_retry_before_press_does_not_duplicate(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_on_call={"open_chat": 1}, fail_mode="timeout")
    adapter = _adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "先失败再成功", send_id="msg-retry")
    # 第一次 open_chat 超时，尚未 press，允许重试后成功
    assert result.ok
    assert result.attempts >= 2
    assert len(driver.pressed) == 1


def test_pause_flag_blocks_send(tmp_path: Path):
    flag = tmp_path / "PAUSE"
    flag.write_text("stop", encoding="utf-8")
    adapter = _adapter(tmp_path)
    result = adapter.send("g1", "不应发出")
    assert not result.ok
    assert result.status == UiaSendStatus.PAUSED
    assert adapter.driver.pressed == []  # type: ignore[attr-defined]


def test_safety_lock_logout_dialog_pause_and_no_relogin(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = _adapter(tmp_path, driver=driver)

    driver.locked = True
    r1 = adapter.send("g1", "锁屏")
    assert not r1.ok and r1.status == UiaSendStatus.UI_NOT_READY
    assert adapter.emergency_paused
    assert (tmp_path / "PAUSE").exists()
    assert driver.login_attempts == 0
    # 紧急暂停后即使解锁也不自动恢复
    driver.locked = False
    r1b = adapter.send("g1", "仍应暂停")
    assert r1b.status == UiaSendStatus.PAUSED
    adapter.clear_emergency_pause()

    driver.logged_in = False
    r2 = adapter.send("g1", "掉线")
    assert not r2.ok and r2.status == UiaSendStatus.UI_NOT_READY
    assert "自动重登" in (r2.error or adapter.pause_reason) or "掉线" in (r2.error or "")
    assert driver.login_attempts == 0
    adapter.clear_emergency_pause()
    driver.logged_in = True

    driver.window_visible = False
    r3 = adapter.send("g1", "不可见")
    assert r3.status == UiaSendStatus.UI_NOT_READY
    adapter.clear_emergency_pause()
    driver.window_visible = True

    driver.blocking_dialog = True
    r4 = adapter.send("g1", "验证码")
    assert r4.status == UiaSendStatus.UI_NOT_READY
    assert any(a["code"] == "blocking_dialog" for a in adapter.alerts)
    assert driver.login_attempts == 0
    assert driver.pressed == []


def test_preheat_and_keep_chat_open(tmp_path: Path, clock: FakeClock | None = None):
    clock = FakeClock()
    driver = FakeUiDriver(known_chats={"交易会员群-主群", "二群"})
    adapter = _adapter(
        tmp_path,
        driver=driver,
        clock=clock,
        keep_chat_open=True,
        preheat_on_start=True,
        preheat_groups=["g1"],
        return_to_primary_after_send=True,
        reopen_min_interval_seconds=30,
        primary_group="g1",
        group_names={"g1": "交易会员群-主群", "g2": "二群"},
    )
    adapter.start()
    assert driver.current_chat() == "交易会员群-主群"
    # 向已打开会话发送，chat_was_open 应为 True
    result = adapter.send("g1", "已打开")
    assert result.ok
    assert result.timings.chat_was_open is True

    # 切到二群后再请求保持主群；间隔不够则不切回
    driver.open_chat("二群")
    assert adapter.keep_chat_open("g1") is False
    clock.advance(31)
    assert adapter.keep_chat_open("g1") is True
    assert driver.current_chat() == "交易会员群-主群"


def test_latency_targets_adapter_own_time(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = _adapter(tmp_path, driver=driver)
    driver.open_chat("交易会员群-主群")
    opened = adapter.send("g1", "已打开会话")
    assert opened.ok
    assert opened.timings.chat_was_open is True
    assert opened.timings.adapter_total <= 3.0

    driver2 = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter2 = _adapter(tmp_path, driver=driver2)
    unopened = adapter2.send("g1", "未打开会话")
    assert unopened.ok
    assert unopened.timings.chat_was_open is False
    assert unopened.timings.adapter_total <= 8.0


def test_humanized_sender_uses_adapter_without_bypass(tmp_path: Path):
    """发送仍走 HumanizedSender，适配器不会提供绕过限频的捷径。"""
    clock = FakeClock()
    adapter = _adapter(tmp_path, clock=clock)
    # 包装成 Composite 以便 HumanizedSender 取 send_text
    inbound = MockAdapter(clock=clock)
    combo = CompositeAdapter(inbound, adapter)
    limiter = RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(8, 0.25),
            per_group=BucketSpec(3, 0.1),
            announcement=BucketSpec(4, 0.1),
            auto_reply=BucketSpec(6, 0.2),
            global_replies_per_hour=40,
        ),
        clock=clock,
    )
    sender = HumanizedSender(
        combo,
        limiter,
        config=HumanizeConfig(
            send_delay_min=2.0,
            send_delay_max=2.0,
            typing_hook=False,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
    )
    sender.submit_auto_reply("g1", "经限速层发出")
    sent = sender.pump(max_items=1)
    assert sent == 1
    assert adapter.sent_messages[0].text == "经限速层发出"
    # 限速层的拟人延迟被 FakeClock 记录，且不计入适配器自身耗时
    assert any(s >= 2.0 for s in clock.sleeps)
    assert adapter.last_result is not None
    assert adapter.last_result.timings.adapter_total < 2.0


def test_factory_builds_uia_with_mock_inbound(tmp_path: Path):
    assert OUTBOUND_UIA in IMPLEMENTED_OUTBOUND
    assert OUTBOUND_UIA not in PENDING_SLOTS
    adapter = build_adapter(
        {
            "wechat": {"inbound": "mock", "outbound": "uia"},
            "bot": {"data_dir": str(tmp_path)},
            "outbound": {"uia": {"data_dir": str(tmp_path), "pause_flag": str(tmp_path / "PAUSE"), "preheat_on_start": False}},
        }
    )
    assert isinstance(adapter, CompositeAdapter)
    assert isinstance(adapter.inbound, MockAdapter)
    assert isinstance(adapter.outbound, UiaOutboundAdapter)
    adapter.send_text("交易会员群-主群", "工厂组装发送")
    assert adapter.outbound.last_result is not None
    assert adapter.outbound.last_result.ok


def test_pyweixin_driver_imports_on_linux_and_hints():
    from src.adapters.pyweixin_driver import PyWeixinDriver, describe_backend

    info = describe_backend()
    assert "platform" in info
    driver = PyWeixinDriver()
    if sys.platform != "win32":
        with pytest.raises(UiaDependencyError, match="Windows"):
            driver.find_chat_window()
        probe = driver.probe()
        assert probe.get("is_windows") is False
        assert "Linux box" in (probe.get("hint") or "") or "Windows" in (probe.get("hint") or "")


def test_group_name_mapping(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群演示"})
    adapter = _adapter(tmp_path, driver=driver, group_names={"demo-group": "交易会员群演示"})
    result = adapter.send("demo-group", "映射后发送")
    assert result.ok
    assert driver.opened == ["交易会员群演示"]


def test_config_from_mapping_reads_outbound_uia():
    cfg = UiaConfig.from_mapping(
        {
            "bot": {"data_dir": "data"},
            "groups": [{"group_id": "demo-group", "name": "交易会员群演示"}],
            "outbound": {
                "uia": {
                    "keep_chat_open": True,
                    "preheat_groups": ["demo-group"],
                    "target_open_chat_seconds": 3,
                    "group_names": {"g2": "二群"},
                }
            },
        }
    )
    assert cfg.group_names["demo-group"] == "交易会员群演示"
    assert cfg.group_names["g2"] == "二群"
    assert cfg.keep_chat_open is True
    assert cfg.preheat_groups == ["demo-group"]
    assert cfg.target_open_chat_seconds == 3


def test_selfcheck_script_dry_run_succeeds(tmp_path: Path, monkeypatch):
    from scripts import selfcheck_uia

    root = Path("/workspace/wechat-community-bot")
    monkeypatch.chdir(root)
    code = selfcheck_uia.main([])
    assert code == 0
    # 未确认时 --send-test 必须拒绝
    code2 = selfcheck_uia.main(["--send-test"])
    assert code2 != 0


def test_default_sender_does_not_touch_repo_pause():
    """UiaSender() 无参构造（契约测试入口）不得创建仓库 data/PAUSE。"""
    repo_pause = Path("/workspace/wechat-community-bot/data/PAUSE")
    repo_alerts = Path("/workspace/wechat-community-bot/data/uia_alerts.jsonl")
    existed_pause = repo_pause.exists()
    existed_alerts = repo_alerts.exists()
    sender = UiaSender(fail_mode="ui_not_ready")
    result = sender.send("交易会员群-主群", "隔离探测")
    assert not result.ok
    assert result.status == UiaSendStatus.UI_NOT_READY
    if not existed_pause:
        assert not repo_pause.exists()
    if not existed_alerts:
        assert not repo_alerts.exists()
    # 暂停文件应落在适配器自己的临时目录
    flag = sender.adapter.config.resolve_pause_flag(sender.adapter.project_root)
    assert flag.exists()
    assert "data/PAUSE" not in str(flag)
    assert Path("/workspace/wechat-community-bot/data") not in flag.parents and flag != Path("/workspace/wechat-community-bot/data/PAUSE")


def test_wechat_bot_pause_file_env_is_honored(tmp_path: Path, monkeypatch):
    env_flag = tmp_path / "env-pause"
    monkeypatch.setenv("WECHAT_BOT_PAUSE_FILE", str(env_flag))
    cfg = UiaConfig(data_dir=str(tmp_path / "data"), pause_flag="data/PAUSE")
    resolved = cfg.resolve_pause_flag(tmp_path)
    assert resolved == env_flag


def test_titles_match_uses_nfkc_and_rejects_prefix():
    """全半角、空白、大小写规范化后精确匹配；同名前缀不得命中。"""
    from src.adapters.textnorm import normalize_ui_text

    assert titles_match("交易会员群－主群", "交易会员群-主群")
    assert titles_match("  交易会员群-主群  ", "交易会员群-主群")
    assert titles_match("交易会员群\u3000主群", "交易会员群 主群")
    assert titles_match("Demo Group", "demo group")
    assert not titles_match("交易会员群", "交易会员群-主群")
    assert not titles_match("交易会员群-主", "交易会员群-主群")
    assert not titles_match("", "交易会员群-主群")
    assert not titles_match("", "")
    assert normalize_chat_title(" 交易会员群－主群 ") == normalize_ui_text("交易会员群-主群")


def test_fullwidth_title_sends_to_correct_chat(tmp_path: Path):
    class WidthTitleDriver(FakeUiDriver):
        def read_chat_title(self) -> str | None:
            if self.current == "交易会员群-主群":
                return "交易会员群－主群"
            return self.current

    driver = WidthTitleDriver(known_chats={"交易会员群-主群"})
    adapter = _adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "全半角应能匹配")
    assert result.ok, result.error
    assert driver.pressed == ["交易会员群-主群"]


def test_paste_runtime_error_is_contained_and_writes_pause(tmp_path: Path):
    class BoomPaste(FakeUiDriver):
        def paste_text(self, text: str) -> None:
            raise RuntimeError("粘贴时意外失败")

    driver = BoomPaste(known_chats={"交易会员群-主群"})
    adapter = _adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "异常应收敛")
    assert result is not None
    assert not result.ok
    assert result.status in {UiaSendStatus.UI_NOT_READY, UiaSendStatus.PAUSED, UiaSendStatus.UNKNOWN}
    assert (tmp_path / "PAUSE").exists()
    assert driver.pressed == []


def test_press_send_runtime_error_is_maybe_sent_and_not_retried(tmp_path: Path):
    class BoomPress(FakeUiDriver):
        def press_send(self) -> None:
            raise RuntimeError("按下时意外失败")

    driver = BoomPress(known_chats={"交易会员群-主群"})
    adapter = _adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "按下后异常", send_id="press-boom")
    assert result is not None
    assert not result.ok
    assert result.status == UiaSendStatus.MAYBE_SENT
    assert (tmp_path / "PAUSE").exists()
    # 同一 send_id 不得重试
    again = adapter.send("g1", "按下后异常", send_id="press-boom")
    assert again.skipped
    assert again.status == UiaSendStatus.MAYBE_SENT


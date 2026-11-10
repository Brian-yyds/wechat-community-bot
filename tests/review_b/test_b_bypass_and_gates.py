# -*- coding: utf-8 -*-
"""限速层与运行模式出口：直接调用是否可绕过；生产发送是否记录失败。"""
from __future__ import annotations

from pathlib import Path

from src.adapters.composite import CompositeAdapter
from src.adapters.mock import MockAdapter
from src.adapters.pause import PauseGate
from src.adapters.ui_driver import FakeUiDriver
from src.clock import FakeClock
from src.latency import LatencyStore, LatencyTrace
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig, SendKind
from src.run_mode import RunMode, SendGate

from review_b.helpers import make_adapter


def _tiny_limiter(clock) -> RateLimiter:
    return RateLimiter(
        RateLimitConfig(
            global_bucket=BucketSpec(8, 0.25),
            per_group=BucketSpec(3, 0.1),
            announcement=BucketSpec(4, 0.1),
            auto_reply=BucketSpec(6, 0.2),
            global_replies_per_hour=40,
        ),
        clock=clock,
    )


def _humanized(adapter, clock, tmp_path, *, mode=RunMode.REPLY, with_gate=True, store=None):
    limiter = _tiny_limiter(clock)
    gate = SendGate(mode=mode, pause_gate=PauseGate(tmp_path / "GATE_PAUSE")) if with_gate else None
    return HumanizedSender(
        adapter,
        limiter,
        config=HumanizeConfig(
            send_delay_min=0.0,
            send_delay_max=0.0,
            typing_hook=False,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
        send_gate=gate,
        latency_store=store,
    )


def test_official_path_through_humanized_sender_is_gated(tmp_path: Path):
    """经 HumanizedSender + SendGate 的正式出口，observe 模式不得发出。"""
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock=clock)
    sender = _humanized(adapter, clock, tmp_path, mode=RunMode.OBSERVE)
    sender.submit_auto_reply("g1", "observe 下不应发出")
    sent = sender.pump(max_items=1)
    assert sent == 0
    assert adapter.driver.pressed == []  # type: ignore[attr-defined]
    assert sender.dropped_count >= 1


def test_direct_adapter_send_bypasses_rate_limiter_and_run_mode(tmp_path: Path):
    """回归：挂接 HumanizedSender 后，直接调用 send / send_text / CompositeAdapter.send_text
    必须被运行模式与限速闸门拦截，不得在 observe 下发出。

    本用例由首轮「绕过存在」的缺陷证据改写而来：修复后直接 send 在 observe 下应被拒绝。
    """
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock=clock)
    sender = _humanized(adapter, clock, tmp_path, mode=RunMode.OBSERVE)
    # 正式出口被挡住
    sender.submit_auto_reply("g1", "走闸门")
    assert sender.pump(max_items=1) == 0

    # 路径 1：适配器 send()
    r1 = adapter.send("g1", "直接 send 必须被拦截")
    # 路径 2：send_text()
    adapter.send_text("g1", "直接 send_text 必须被拦截")
    # 路径 3：先挂接组合适配器再构造发送器，覆盖 CompositeAdapter 出口
    combo = CompositeAdapter(MockAdapter(clock=clock), adapter)
    combo_sender = _humanized(combo, clock, tmp_path, mode=RunMode.OBSERVE)
    combo.send_text("g1", "composite send_text 必须被拦截")
    r_combo = adapter.last_result

    assert r1 is not None and not r1.ok, f"直接 send 在 observe 下应被闸门拒绝，实际 {r1}"
    assert r1.status in {"rejected", "paused"}, f"直接 send 状态应为 rejected/paused，实际 {r1.status}"
    assert r_combo is not None and not r_combo.ok, (
        f"CompositeAdapter.send_text 在 observe 下应被拒绝，实际 {r_combo}"
    )
    pressed = list(adapter.driver.pressed)  # type: ignore[attr-defined]
    assert pressed == [], (
        "直接调用适配器/组合适配器仍在 observe 模式下发出，闸门未拦截："
        f" 实际按下 {pressed}"
    )
    assert combo_sender.dropped_count >= 0


def test_driver_paste_and_press_bypass_all_gates(tmp_path: Path):
    """证据：直接调用 UiDriver.paste_text / press_send 完全绕过暂停、限速、幂等与运行模式。"""
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    driver.open_chat("交易会员群-主群")
    # 即便仓库/临时 PAUSE 已存在，驱动层也不会看
    (tmp_path / "PAUSE").write_text("stop", encoding="utf-8")
    driver.paste_text("驱动层直接粘贴")
    driver.press_send()
    assert driver.pasted == ["驱动层直接粘贴"]
    assert driver.pressed == ["交易会员群-主群"]


def test_pause_flag_is_not_rechecked_before_paste_or_press(tmp_path: Path):
    """pause.py 约定：真正粘贴/点击发送之前必须再检查暂停。当前实现只在 send() 开头检查一次。"""

    class PauseAfterFocusDriver(FakeUiDriver):
        def __init__(self, flag: Path, **kwargs):
            super().__init__(**kwargs)
            self.flag = flag

        def focus_input(self) -> None:
            self.flag.write_text("mid-send pause", encoding="utf-8")
            super().focus_input()

    flag = tmp_path / "PAUSE"
    driver = PauseAfterFocusDriver(flag, known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    result = adapter.send("g1", "焦点后被暂停，不应再粘贴或发送")
    assert not result.ok, "中途出现 PAUSE 后发送应失败"
    assert result.status == "paused"
    assert driver.pasted == [], f"暂停后仍执行了 paste_text: {driver.pasted}"
    assert driver.pressed == [], f"暂停后仍执行了 press_send: {driver.pressed}"


def test_humanized_sender_treats_failed_uia_send_as_success(tmp_path: Path):
    """生产路径 HumanizedSender.send_text 忽略 last_result：失败/maybe_sent 仍记为 sent。"""
    clock = FakeClock()
    driver = FakeUiDriver(known_chats={"交易会员群-主群"}, fail_mode="rejected")
    adapter = make_adapter(tmp_path, driver=driver, clock=clock)
    store = LatencyStore(tmp_path / "latency.jsonl")
    sender = _humanized(adapter, clock, tmp_path, store=store)
    trace = LatencyTrace(message_id="fail-1", group_id="g1", kind="auto_reply")
    sender.submit_auto_reply("g1", "校验失败仍被记成功", trace=trace)
    sent = sender.pump(max_items=1)
    assert adapter.last_result is not None
    assert adapter.last_result.ok is False
    records = store.read_all()
    assert records, "应写入 latency.jsonl"
    # 正确行为：不得把失败发送记成 outcome=sent，sent_count 也不应增加
    assert sent == 0, f"UIA 校验失败时 HumanizedSender 仍返回 sent={sent}"
    assert sender.sent_count == 0, f"失败发送被计入 sent_count={sender.sent_count}"
    assert records[-1].get("outcome") != "sent", (
        f"失败发送被写入 latency.jsonl 且 outcome={records[-1].get('outcome')}"
    )


def test_humanized_sender_does_not_pass_send_id(tmp_path: Path):
    """生产路径不传 send_id，导致 UIA 层幂等在正式出口失效。"""
    clock = FakeClock()
    adapter = make_adapter(tmp_path, clock=clock)
    sender = _humanized(adapter, clock, tmp_path)
    sender.submit_auto_reply("g1", "同一条正式出口")
    sender.submit_auto_reply("g1", "同一条正式出口")
    sent = sender.pump(max_items=2)
    pressed = adapter.driver.pressed  # type: ignore[attr-defined]
    # 正确行为：应由上层传入稳定 send_id，避免同文被按下两次
    assert sent == 1, f"无 send_id 时正式出口把相同文案发了 {sent} 次"
    assert len(pressed) == 1, f"同一条文被按下 {len(pressed)} 次，幂等未接入生产出口"

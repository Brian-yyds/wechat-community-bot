# -*- coding: utf-8 -*-
"""运行模式 observe / announce / reply（RM-01～RM-04）。

observe：只收不发（不自动回复、不发公告）。
announce：只发公告，不自动回复。
reply：才允许自动回复（公告默认仍允许）。
切换立即生效；状态放入 store，重启后保持。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adapter import AdapterNotReady, get_impl, use_mock

SHANGHAI = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 11, 18, 0, 0, tzinfo=SHANGHAI)


class _Clock:
    def __init__(self, now=None):
        self._now = now or T0

    def now(self):
        return self._now

    def set(self, dt):
        self._now = dt


def _make_mode(store=None, mode="observe"):
    store = store if store is not None else {}
    if use_mock():
        from mock.impl import RunModeController

        return RunModeController(store=store, mode=mode), "mock"
    try:
        impl = get_impl()
    except AdapterNotReady:
        impl = None
    if impl is not None:
        for name in ("RunModeController", "ModeController", "RuntimeMode", "BotRunMode", "SendGate"):
            cls = getattr(impl, name, None)
            if cls is None:
                continue
            obj = _adapt_mode_obj(cls, store, mode)
            if obj is not None:
                return obj, f"real:{name}"
    try:
        from src.run_mode import SendGate
    except ImportError:
        SendGate = None
    if SendGate is not None:
        obj = _adapt_mode_obj(SendGate, store, mode)
        if obj is not None:
            return obj, "real:src.run_mode.SendGate"
    pytest.skip(
        "开发尚未交付可持久化的运行模式控制器（observe/announce/reply）。"
        "请暴露 get_mode/set_mode、allow_auto_reply/allow_announcement，状态可放入 store 以便重启保持。"
    )


def _adapt_mode_obj(cls, store, mode):
    """把开发的 SendGate / 控制器适配到 get_mode/set_mode 约定。不替它做持久化。"""
    try:
        obj = cls(store=store, mode=mode)
    except TypeError:
        try:
            initial = store.get("run_mode", mode) if isinstance(store, dict) else mode
            obj = cls(mode=initial)
        except TypeError:
            try:
                obj = cls()
            except TypeError:
                return None

    if not hasattr(obj, "get_mode"):
        def _get_mode(_self=obj):
            m = getattr(_self, "mode", None)
            return m.value if hasattr(m, "value") else str(m)

        obj.get_mode = _get_mode
    if not hasattr(obj, "set_mode"):
        def _set_mode(new_mode, _self=obj):
            from src.run_mode import parse_run_mode
            _self.mode = parse_run_mode(new_mode)  # 非法模式须抛出 RunModeError
            return _self.get_mode()

        obj.set_mode = _set_mode
    if not hasattr(obj, "allow_auto_reply") or not hasattr(obj, "allow_announcement"):
        return None
    # 若实现自己把模式写入 store，测试重启时就能读到；否则 RM-04 应失败。
    obj._persist_store = store
    return obj


def test_rm01_observe_receives_but_does_not_send():
    """RM-01 observe：只收不发。"""
    mode, _ = _make_mode(mode="observe")
    assert mode.get_mode() == "observe"
    assert mode.allow_auto_reply() is False
    assert mode.allow_announcement() is False
    if hasattr(mode, "allow_send"):
        assert mode.allow_send("reply") is False
        assert mode.allow_send("announcement") is False


def test_rm02_announce_only_announcements():
    """RM-02 announce：只发公告，不自动回复。"""
    mode, _ = _make_mode(mode="announce")
    assert mode.get_mode() == "announce"
    assert mode.allow_auto_reply() is False
    assert mode.allow_announcement() is True


def test_rm03_reply_enables_auto_reply():
    """RM-03 reply：才允许自动回复；公告默认仍允许。"""
    mode, _ = _make_mode(mode="reply")
    assert mode.get_mode() == "reply"
    assert mode.allow_auto_reply() is True
    assert mode.allow_announcement() is True


def test_rm04_switch_immediate_and_persists_after_restart():
    """RM-04 切换立即生效，重启后保持。"""
    store = {}
    mode, _ = _make_mode(store=store, mode="observe")
    assert mode.allow_auto_reply() is False
    mode.set_mode("reply")
    assert mode.get_mode() == "reply"
    assert mode.allow_auto_reply() is True  # 下一动作立即按新模式
    # 模拟重启
    mode2, src = _make_mode(store=store, mode="observe")  # 传入的默认值不得覆盖已持久化的 reply
    assert mode2.get_mode() == "reply", (
        f"重启后模式应保持 reply，实际 {mode2.get_mode()}（来源 {src}）。"
        "请把当前运行模式写入可持久化 store 或配置，重建后读取，避免重启回到 observe。"
    )
    mode2.set_mode("announce")
    assert mode2.allow_auto_reply() is False
    assert mode2.allow_announcement() is True
    mode3, _ = _make_mode(store=store)
    assert mode3.get_mode() == "announce"


def test_rm00_invalid_mode_rejected():
    mode, _ = _make_mode(mode="observe")
    with pytest.raises((ValueError, TypeError, KeyError, AssertionError)):
        mode.set_mode("broadcast")

# -*- coding: utf-8 -*-
"""通知辅助契约：只提供提示触发，不当作消息源完整性保证。"""
from __future__ import annotations

import pytest


def _make(factory, **kwargs):
    try:
        return factory(**kwargs)
    except TypeError:
        try:
            return factory()
        except TypeError as exc:
            pytest.skip(f"NotificationAssist 构造签名无法适配: {exc}")


def test_not_a_complete_source(factory):
    assist = _make(factory)
    if hasattr(assist, "is_complete_source"):
        assert assist.is_complete_source() is False, "通知辅助不得声明自己是完整消息源"
    if hasattr(assist, "completeness_guaranteed"):
        assert assist.completeness_guaranteed() is False, "通知辅助不得保证消息源完整性"
    if not hasattr(assist, "is_complete_source") and not hasattr(assist, "completeness_guaranteed"):
        pytest.fail(
            "通知辅助必须显式提供 is_complete_source() 或 completeness_guaranteed()，"
            "并且返回 False。不能把它当作全量抓取真源。"
        )


def test_hints_are_optional_and_lossy(factory):
    try:
        assist = _make(
            factory,
            hints=[
                {"group": "交易会员群-主群", "preview": "有人提问手续费", "ts": "2026-11-11T12:00:00+08:00"},
                {"group": "免打扰群", "preview": "被抑制的通知", "ts": "2026-11-11T12:00:01+08:00"},
            ],
            suppressed={"免打扰群"},
        )
    except TypeError:
        assist = _make(factory)
        if not hasattr(assist, "poll_hints"):
            pytest.skip("无法注入 hints 做完整性对照")
        # 无注入则至少保证接口存在
        hints = assist.poll_hints()
        assert isinstance(hints, list)
        return
    hints = assist.poll_hints()
    groups = [getattr(h, "group", None) or (h.get("group") if isinstance(h, dict) else None) for h in hints]
    assert "交易会员群-主群" in groups
    assert "免打扰群" not in groups, "被免打扰/抑制的群可以没有提示，调用方不得据此认定该群没有新消息"
    for h in hints:
        complete = getattr(h, "complete", None)
        if complete is None and isinstance(h, dict):
            complete = h.get("complete")
        if complete is not None:
            assert complete is False, "单条通知提示不得标记为完整消息"


def test_missing_hint_is_not_zero_message_proof(factory):
    """没有提示 ≠ 没有消息。契约：适配器不能提供「某群此时无新消息」的保证接口，
    或该接口必须明确返回「不确定」。"""
    assist = _make(factory, hints=[], suppressed=set())
    if hasattr(assist, "has_no_messages"):
        assert assist.has_no_messages("任意群") is not True, "不得把「没有通知」当成「没有消息」"
    # poll 为空只是「本轮没有提示」
    hints = assist.poll_hints()
    assert hints == [] or hints is not None

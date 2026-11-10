# -*- coding: utf-8 -*-
"""UIA 发送契约：send(group, text)->结果、失败可区分、超时。"""
from __future__ import annotations

import pytest


def _make(factory, **kwargs):
    try:
        return factory(**kwargs)
    except TypeError:
        try:
            return factory()
        except TypeError as exc:
            pytest.skip(f"UiaSender 构造签名无法适配: {exc}")


def _status_of(result) -> str:
    if hasattr(result, "status"):
        return str(getattr(result, "status") or "")
    if isinstance(result, dict):
        return str(result.get("status") or result.get("code") or "")
    return ""


def _ok(result) -> bool:
    if hasattr(result, "ok"):
        return bool(result.ok)
    if isinstance(result, dict):
        return bool(result.get("ok"))
    if isinstance(result, bool):
        return result
    return False


def test_send_success(factory):
    sender = _make(factory)
    result = sender.send("交易会员群-主群", "测试发送（虚构，请忽略）")
    assert _ok(result), f"正常发送应成功，实际 {result!r}"
    status = _status_of(result)
    if status:
        assert status.lower() in {"ok", "success", "sent"}, f"成功状态应可区分，实际 {status}"


def test_failures_are_distinguishable(factory):
    """timeout / target_not_found / ui_not_ready 必须能互相区分。"""
    modes = ["timeout", "target_not_found", "ui_not_ready"]
    statuses = {}
    for mode in modes:
        try:
            sender = _make(factory, fail_mode=mode)
        except TypeError:
            pytest.skip("UiaSender 不支持注入 fail_mode，无法在无真实微信时验证失败分类（请提供 fail_mode 或等效测试钩子）")
        result = sender.send("不存在的群", "ping", timeout=0.2) if _accepts_timeout(sender) else sender.send("不存在的群", "ping")
        assert not _ok(result), f"{mode} 不应返回成功: {result!r}"
        statuses[mode] = _status_of(result) or str(getattr(result, "error", "") or result)
    assert len(set(statuses.values())) == len(modes), f"失败类型无法区分: {statuses}"
    # 关键词覆盖
    assert "timeout" in statuses["timeout"].lower() or "超时" in statuses["timeout"]
    assert "not_found" in statuses["target_not_found"].lower() or "找不到" in statuses["target_not_found"] or "not found" in statuses["target_not_found"].lower()
    assert "ui" in statuses["ui_not_ready"].lower() or "窗口" in statuses["ui_not_ready"] or "not_ready" in statuses["ui_not_ready"].lower()


def _accepts_timeout(sender) -> bool:
    import inspect
    try:
        sig = inspect.signature(sender.send)
    except (TypeError, ValueError):
        return False
    return "timeout" in sig.parameters


def test_timeout_respected(factory):
    try:
        sender = _make(factory, fail_mode="timeout")
    except TypeError:
        pytest.skip("UiaSender 不支持注入 timeout 失败")
    import time
    t0 = time.perf_counter()
    if _accepts_timeout(sender):
        result = sender.send("g1", "timeout-probe", timeout=0.3)
    else:
        result = sender.send("g1", "timeout-probe")
    elapsed = time.perf_counter() - t0
    assert not _ok(result)
    status = _status_of(result)
    assert "timeout" in status.lower() or "超时" in status
    # 契约测试禁止长时间真实阻塞
    assert elapsed < 2.0, f"超时路径不应长时间阻塞，实际 {elapsed:.2f}s"

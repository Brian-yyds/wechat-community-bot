"""按配置创建适配器。读取与发送可分开指定，未实现的角色会给出明确错误。"""

from __future__ import annotations

import warnings

from collections.abc import Callable
from typing import Any

from src.adapters.base import Adapter, Group, InboundAdapter, OutboundAdapter
from src.adapters.composite import CompositeAdapter
from src.adapters.mock import MockAdapter
from src.adapters.roles import (
    IMPLEMENTED_INBOUND,
    IMPLEMENTED_OUTBOUND,
    INBOUND_LOCAL_CAPTURE,
    INBOUND_MOCK,
    INBOUND_NOTIFICATION,
    OUTBOUND_MOCK,
    OUTBOUND_UIA,
    OUTBOUND_OCR,
    PENDING_SLOTS,
)
from src.clock import Clock, SystemClock


class AdapterNotImplementedError(RuntimeError):
    """配置了尚未交付的适配器角色。"""


def build_adapter(
    config: dict[str, Any] | None = None,
    *,
    groups: list[Group] | None = None,
    clock: Clock | None = None,
    wake_callback: Callable | None = None,
    wake_target: Any | None = None,
) -> Adapter:
    """根据 wechat.inbound / wechat.outbound（缺省均为 mock）组装适配器。

    可选把通知旁路（C）挂到 CompositeAdapter.notification，用于唤醒主读取适配器（A）。
    """
    config = config or {}
    section = config.get("wechat") or {}
    inbound_name = str(section.get("inbound") or section.get("protocol") or INBOUND_MOCK).strip()
    outbound_name = str(section.get("outbound") or section.get("protocol") or OUTBOUND_MOCK).strip()
    clock = clock or SystemClock()

    notify_cfg = {}
    for candidate in (
        section.get("notification") if isinstance(section.get("notification"), dict) else None,
        (config.get("inbound") or {}).get("notification") if isinstance(config.get("inbound"), dict) else None,
    ):
        if isinstance(candidate, dict):
            notify_cfg.update(candidate)
    assist_wanted = bool(notify_cfg.get("enabled")) or str(section.get("assist") or "").strip() == INBOUND_NOTIFICATION
    # 与文档 / example 默认值一致：未写时视为 True。
    if "wake_a_on_preview" in notify_cfg:
        wake_a_on_preview = bool(notify_cfg.get("wake_a_on_preview"))
    else:
        wake_a_on_preview = True
    # 主读取已经是 notification 时不必再挂一份。
    if inbound_name == INBOUND_NOTIFICATION:
        assist_wanted = False
        warnings.warn(
            "wechat.inbound=notification 不能作为唯一消息源。"
            "预览可能被截断，只应用于唤醒或记录；请与 local_capture 组合。"
            "管线不会对 partial=True 的预览自动回复。",
            UserWarning,
            stacklevel=2,
        )

    if (
        inbound_name == INBOUND_MOCK
        and outbound_name == OUTBOUND_MOCK
        and not assist_wanted
        and wake_callback is None
        and wake_target is None
    ):
        return MockAdapter(groups=groups, clock=clock)

    inbound = _create_inbound(
        inbound_name,
        groups=groups,
        clock=clock,
        config=config,
        wake_callback=wake_callback,
    )
    outbound = _create_outbound(outbound_name, groups=groups, clock=clock, config=config)

    notification = None
    if assist_wanted:
        notification = _create_inbound(
            INBOUND_NOTIFICATION,
            groups=groups,
            clock=clock,
            config=config,
        )
        # 只有 wake_a_on_preview=true（默认）时才自动把唤醒钩子接到 A。
        # 为 false 时不得设置 wake_target，也不得给通知适配器挂默认回调。
        if wake_a_on_preview:
            if wake_target is None and inbound_name != INBOUND_NOTIFICATION:
                wake_target = inbound
        elif wake_callback is None:
            wake_target = None

    if inbound is outbound and notification is None and wake_callback is None and wake_target is None:
        return inbound  # type: ignore[return-value]
    return CompositeAdapter(
        inbound,
        outbound,
        wake_callback=wake_callback,
        wake_target=wake_target,
        notification=notification,
    )


def _pending_message(name: str, kind: str) -> str:
    label = PENDING_SLOTS.get(name, name)
    return (
        f"{kind}适配器「{name}」（{label}）尚未在第一阶段实现。"
        "请保持 mock，或等待后续 A/B/C/D 的 MVP 任务。"
    )


def _create_inbound(
    name: str,
    *,
    groups,
    clock,
    config: dict[str, Any] | None = None,
    wake_callback=None,
) -> InboundAdapter:
    if name == INBOUND_LOCAL_CAPTURE:
        from src.adapters.local_capture import LocalCaptureAdapter

        return LocalCaptureAdapter.from_config(config or {}, groups=groups, clock=clock)
    if name == INBOUND_NOTIFICATION:
        from src.adapters.notification import NotificationAdapter

        return NotificationAdapter.from_config(
            config or {},
            groups=groups,
            clock=clock,
            wake_callback=wake_callback,
        )
    if name not in IMPLEMENTED_INBOUND:
        raise AdapterNotImplementedError(_pending_message(name, "读取"))
    return MockAdapter(groups=groups, clock=clock)


def _create_outbound(name: str, *, groups, clock, config: dict[str, Any] | None = None) -> OutboundAdapter:
    if name == OUTBOUND_UIA:
        from src.adapters.uia import UiaOutboundAdapter

        return UiaOutboundAdapter.from_config(config or {}, groups=groups, clock=clock)
    if name == OUTBOUND_OCR:
        from src.adapters.ocr import OcrOutboundAdapter

        return OcrOutboundAdapter.from_config(config or {}, clock=clock)
    if name not in IMPLEMENTED_OUTBOUND:
        raise AdapterNotImplementedError(_pending_message(name, "发送"))
    return MockAdapter(groups=groups, clock=clock)

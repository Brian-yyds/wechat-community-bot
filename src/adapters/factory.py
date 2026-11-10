"""按配置创建适配器。读取与发送可分开指定，未实现的角色会给出明确错误。"""

from __future__ import annotations

from typing import Any

from src.adapters.base import Adapter, Group, InboundAdapter, OutboundAdapter
from src.adapters.composite import CompositeAdapter
from src.adapters.mock import MockAdapter
from src.adapters.roles import (
    IMPLEMENTED_INBOUND,
    IMPLEMENTED_OUTBOUND,
    INBOUND_MOCK,
    OUTBOUND_MOCK,
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
) -> Adapter:
    """根据 wechat.inbound / wechat.outbound（缺省均为 mock）组装适配器。"""
    section = (config or {}).get("wechat") or {}
    inbound_name = str(section.get("inbound") or section.get("protocol") or INBOUND_MOCK).strip()
    outbound_name = str(section.get("outbound") or section.get("protocol") or OUTBOUND_MOCK).strip()
    clock = clock or SystemClock()

    if inbound_name == INBOUND_MOCK and outbound_name == OUTBOUND_MOCK:
        return MockAdapter(groups=groups, clock=clock)

    inbound = _create_inbound(inbound_name, groups=groups, clock=clock)
    outbound = _create_outbound(outbound_name, groups=groups, clock=clock)
    if inbound is outbound:
        return inbound  # type: ignore[return-value]
    return CompositeAdapter(inbound, outbound)


def _pending_message(name: str, kind: str) -> str:
    label = PENDING_SLOTS.get(name, name)
    return (
        f"{kind}适配器「{name}」（{label}）尚未在第一阶段实现。"
        "请保持 mock，或等待后续 A/B/C/D 的 MVP 任务。"
    )


def _create_inbound(name: str, *, groups, clock) -> InboundAdapter:
    if name not in IMPLEMENTED_INBOUND:
        raise AdapterNotImplementedError(_pending_message(name, "读取"))
    return MockAdapter(groups=groups, clock=clock)


def _create_outbound(name: str, *, groups, clock) -> OutboundAdapter:
    if name not in IMPLEMENTED_OUTBOUND:
        raise AdapterNotImplementedError(_pending_message(name, "发送"))
    return MockAdapter(groups=groups, clock=clock)

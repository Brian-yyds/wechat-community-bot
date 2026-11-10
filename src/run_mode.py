"""运行模式与发送出口闸门。

配置项 ``run.mode``（改这一行即可切换）：

- observe：只读不发（默认）
- announce：只发定时公告，不自动回复
- reply：自动回复 + 定时公告

紧急暂停：``data/PAUSE`` 文件存在时，任何发送都不执行。
这与适配器 B（UIA 发送）、D（OCR 降级发送）使用同一约定
（见 ``src.adapters.pause.PauseGate``）。文件存在即暂停，不解析内容。

模式与暂停必须在发送出口强制执行：上游即使误提交，也不真正发出。
不允许的发送一律丢弃并记日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.adapters.pause import PauseGate


class RunMode(str, Enum):
    """运行模式。"""

    OBSERVE = "observe"
    ANNOUNCE = "announce"
    REPLY = "reply"


VALID_MODES = tuple(mode.value for mode in RunMode)

# 与发送类型名保持一致，便于发送器直接传入。
KIND_AUTO_REPLY = "auto_reply"
KIND_ANNOUNCEMENT = "announcement"


class RunModeError(ValueError):
    """运行模式配置非法。"""


def parse_run_mode(value: object | None, *, default: RunMode = RunMode.OBSERVE) -> RunMode:
    """解析运行模式，空值使用默认值 observe。"""
    if value is None:
        return default
    if isinstance(value, RunMode):
        return value
    # Python 3.13 中 str(StrEnum 成员) 可能是 "RunMode.OBSERVE"，必须用 .value。
    raw = value.value if isinstance(value, Enum) else value
    text = str(raw).strip().lower()
    if not text:
        return default
    try:
        return RunMode(text)
    except ValueError as exc:
        raise RunModeError(
            f"未知运行模式 {value!r}，应为 observe / announce / reply"
        ) from exc


def mode_allows(mode: RunMode | str, kind: str) -> bool:
    """该模式是否允许发出指定类型的消息。"""
    if not isinstance(mode, RunMode):
        mode = parse_run_mode(mode)
    normalized = _normalize_kind(kind)
    if normalized == KIND_ANNOUNCEMENT:
        return mode in {RunMode.ANNOUNCE, RunMode.REPLY}
    # 自动回复以及其他主动发送，仅 reply 模式允许。
    return mode is RunMode.REPLY


def _normalize_kind(kind: object) -> str:
    value = getattr(kind, "value", kind)
    text = str(value or KIND_AUTO_REPLY).strip().lower()
    if text in {"announcement", "announce"}:
        return KIND_ANNOUNCEMENT
    return KIND_AUTO_REPLY


@dataclass(frozen=True)
class GateDecision:
    """发送出口的一次判定。"""

    allowed: bool
    reason: str
    paused: bool = False
    mode: str = RunMode.OBSERVE.value
    kind: str = KIND_AUTO_REPLY

    @property
    def action(self) -> str:
        return "send" if self.allowed else "drop"


class SendGate:
    """发送出口统一闸门：先看紧急暂停，再看运行模式。

    未注入闸门时，发送器保持原有行为（允许发送），以免破坏既有单测。
    由 ``build_app`` 根据配置装配闸门后，默认模式为 observe。
    """

    def __init__(
        self,
        mode: RunMode | str = RunMode.OBSERVE,
        pause_gate: PauseGate | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.mode = parse_run_mode(mode) if not isinstance(mode, RunMode) else mode
        self.pause_gate = pause_gate
        self.logger = logger or logging.getLogger("wechat_bot")

    def is_paused(self) -> bool:
        return bool(self.pause_gate is not None and self.pause_gate.is_paused())

    def allow_auto_reply(self) -> bool:
        return mode_allows(self.mode, KIND_AUTO_REPLY)

    def allow_announcement(self) -> bool:
        return mode_allows(self.mode, KIND_ANNOUNCEMENT)

    def decide(self, kind: object) -> GateDecision:
        """在发送出口判定是否允许真正发出。"""
        normalized = _normalize_kind(kind)
        if self.is_paused():
            path = getattr(self.pause_gate, "path", Path("data") / "PAUSE")
            return GateDecision(
                allowed=False,
                reason=f"紧急暂停（发现 {path}），丢弃发送",
                paused=True,
                mode=self.mode.value,
                kind=normalized,
            )
        if not mode_allows(self.mode, normalized):
            if self.mode is RunMode.OBSERVE:
                reason = f"运行模式 observe 不允许发送 {normalized}，已丢弃"
            elif normalized == KIND_AUTO_REPLY:
                reason = "运行模式 announce 不允许自动回复，已丢弃"
            else:
                reason = f"运行模式 {self.mode.value} 不允许发送 {normalized}，已丢弃"
            return GateDecision(
                allowed=False,
                reason=reason,
                paused=False,
                mode=self.mode.value,
                kind=normalized,
            )
        return GateDecision(
            allowed=True,
            reason="ok",
            paused=False,
            mode=self.mode.value,
            kind=normalized,
        )

    def log_drop(self, decision: GateDecision, *, group_id: str = "") -> None:
        """记录被闸门丢弃的发送。日志走脱敏过滤器，不写正文。"""
        group = group_id or "-"
        self.logger.info(
            "发送已丢弃：%s；kind=%s group_id=%s mode=%s",
            decision.reason,
            decision.kind,
            group,
            decision.mode,
        )

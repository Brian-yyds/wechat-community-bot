"""微信客户端 UI 操作抽象。

真实 UIA 只能在用户 Windows 电脑上驱动官方 PC 微信窗口。
Linux box 只能使用 FakeUiDriver 做模拟测试。

本模块不依赖 pyweixin / pywinauto，可在任何平台导入。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


def normalize_chat_title(name: str | None) -> str:
    """规范化会话标题：去掉首尾空白并压缩中间空白，便于精确匹配。"""
    return " ".join((name or "").strip().split())


def titles_match(actual: str | None, expected: str | None) -> bool:
    """规范化后严格精确匹配。同名前缀或仅部分相同一律视为不匹配。"""
    left = normalize_chat_title(actual)
    right = normalize_chat_title(expected)
    return bool(left) and left == right


class UiaError(Exception):
    """UIA 发送相关错误的基类。"""

    status: str = "unknown"

    def __init__(self, message: str, *, status: str | None = None) -> None:
        super().__init__(message)
        if status:
            self.status = status


class ChatNotFoundError(UiaError):
    """找不到目标群窗口或会话。"""

    status = "target_not_found"


class SendTimeoutError(UiaError):
    """打开会话、粘贴、发送或校验超时。"""

    status = "timeout"


class UiNotReadyError(UiaError):
    """窗口不可见、锁屏、掉线或出现阻断弹窗，此时必须立即暂停发送。"""

    status = "ui_not_ready"


class UiaDependencyError(UiaError):
    """缺少 Windows 依赖或当前平台无法使用真实驱动。"""

    status = "ui_not_ready"


class UiaPausedError(UiaError):
    """紧急暂停已触发，拒绝发送。"""

    status = "paused"


class UiDriver(ABC):
    """官方 PC 微信窗口操作的最小接口。

    实现方应只做「打开会话 / 输入 / 发送 / 校验 / 状态探活」，
    不要在驱动内做自动反复重登、打码或绕过验证码。
    """

    @abstractmethod
    def find_chat_window(self) -> bool:
        """定位主聊天窗口，成功返回 True。"""

    @abstractmethod
    def open_chat(self, group_name: str) -> None:
        """按显示名打开目标群会话。找不到时抛出 ChatNotFoundError。"""

    @abstractmethod
    def focus_input(self) -> None:
        """把焦点放到输入框。"""

    @abstractmethod
    def paste_text(self, text: str) -> None:
        """把文本写入输入框（剪贴板粘贴或 set_value）。"""

    @abstractmethod
    def press_send(self) -> None:
        """按下发送。调用后不得在未校验的情况下视为失败而重发。"""

    @abstractmethod
    def verify_sent(self, text: str) -> bool:
        """尽力确认刚才的文本已出现在会话中。"""

    @abstractmethod
    def is_logged_in(self) -> bool:
        """是否仍处于已登录状态。掉线时返回 False，不要自动重登。"""

    @abstractmethod
    def is_locked(self) -> bool:
        """操作系统是否锁屏。"""

    @abstractmethod
    def is_window_visible(self) -> bool:
        """微信主窗口是否可见（未被最小化到无法操作、会话未断开）。"""

    def has_blocking_dialog(self) -> bool:
        """是否出现验证码或异常弹窗。默认未知则返回 False，由具体驱动覆盖。"""
        return False

    def current_chat(self) -> str | None:
        """当前已打开的会话显示名；未知时返回 None。

        调用方不要只信内存缓存。发送前应通过 read_chat_title 核对窗口标题。
        """
        return None

    def read_chat_title(self) -> str | None:
        """读取当前窗口中可见的会话标题。默认未知。

        与 current_chat 分开：后者可能只是内存值，本方法应尽量读真实 UI。
        """
        return None

    def probe(self) -> dict[str, Any]:
        """自检用状态快照，缺省根据上述方法拼装。"""
        return {
            "logged_in": self.is_logged_in(),
            "locked": self.is_locked(),
            "window_visible": self.is_window_visible(),
            "blocking_dialog": self.has_blocking_dialog(),
            "current_chat": self.current_chat(),
            "chat_window_found": self.find_chat_window(),
        }

    def close(self) -> None:
        """释放资源。默认空操作。"""
        return None


@dataclass
class FakeUiDriver(UiDriver):
    """供 box / 单元测试使用的假驱动，可注入失败模式与分段延迟。

    fail_mode:
      - timeout
      - target_not_found
      - ui_not_ready
      - rejected（按下发送后校验失败）
    分段延迟通过 stage_delays（秒）注入，测试时钟由上层 sleep；
    本驱动默认不真实阻塞，只记录调用。
    """

    fail_mode: str | None = None
    stage_delays: dict[str, float] = field(default_factory=dict)
    known_chats: set[str] | None = None
    logged_in: bool = True
    locked: bool = False
    window_visible: bool = True
    blocking_dialog: bool = False
    current: str | None = None
    # 记录调用，便于断言预热、发送次数、是否试图重登。
    opened: list[str] = field(default_factory=list)
    focused: int = 0
    pasted: list[str] = field(default_factory=list)
    pressed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    sent_ok: list[tuple[str, str]] = field(default_factory=list)
    login_attempts: int = 0
    probe_calls: int = 0
    fail_on_call: dict[str, int] = field(default_factory=dict)
    _call_counts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        mode = (self.fail_mode or "").strip()
        if mode == "ui_not_ready":
            self.window_visible = False
        # 契约测试默认不限制已知群，除非调用方显式传入 known_chats。

    def _count(self, stage: str) -> int:
        with self._lock:
            self._call_counts[stage] = self._call_counts.get(stage, 0) + 1
            return self._call_counts[stage]

    def _raise_if_scripted(self, stage: str) -> None:
        n = self._count(stage)
        scripted = self.fail_on_call.get(stage)
        if self.fail_on_call:
            # 只在指定阶段的指定次数失败，其它阶段照常成功。
            if scripted is None or n != scripted:
                return
            mode = self.fail_mode or "timeout"
        else:
            mode = self.fail_mode
        if mode == "timeout" and stage in {"open_chat", "paste_text", "press_send", "verify_sent", "find_chat_window"}:
            raise SendTimeoutError(f"模拟超时（阶段 {stage}）")
        if mode == "target_not_found" and stage == "open_chat":
            raise ChatNotFoundError(f"找不到目标群窗口（阶段 {stage}）")
        if mode == "ui_not_ready" and stage in {"find_chat_window", "open_chat", "focus_input"}:
            raise UiNotReadyError("微信窗口不可用（锁屏/最小化/会话断开）")

    def find_chat_window(self) -> bool:
        self._raise_if_scripted("find_chat_window")
        if not self.window_visible or self.locked or not self.logged_in:
            return False
        return True

    def open_chat(self, group_name: str) -> None:
        self._raise_if_scripted("open_chat")
        if self.known_chats is not None and group_name not in self.known_chats:
            raise ChatNotFoundError(f"找不到目标群窗口: {group_name}")
        self.current = group_name
        self.opened.append(group_name)

    def focus_input(self) -> None:
        self._raise_if_scripted("focus_input")
        self.focused += 1

    def paste_text(self, text: str) -> None:
        self._raise_if_scripted("paste_text")
        self.pasted.append(text)

    def press_send(self) -> None:
        self._raise_if_scripted("press_send")
        self.pressed.append(self.current or "")

    def verify_sent(self, text: str) -> bool:
        self._raise_if_scripted("verify_sent")
        self.verified.append(text)
        if self.fail_mode == "rejected":
            return False
        chat = self.current or ""
        self.sent_ok.append((chat, text))
        return True

    def is_logged_in(self) -> bool:
        return bool(self.logged_in)

    def is_locked(self) -> bool:
        return bool(self.locked)

    def is_window_visible(self) -> bool:
        return bool(self.window_visible)

    def has_blocking_dialog(self) -> bool:
        return bool(self.blocking_dialog)

    def current_chat(self) -> str | None:
        return self.current

    def read_chat_title(self) -> str | None:
        """以实际窗口字段为准，不受 current_chat 被包装覆盖的影响。"""
        return self.current

    def probe(self) -> dict[str, Any]:
        self.probe_calls += 1
        return super().probe() | {"backend": "fake", "login_attempts": self.login_attempts}

    def attempt_login(self) -> None:
        """测试探针：真实驱动不得提供自动重登；假驱动只记次数，便于断言未被调用。"""
        self.login_attempts += 1

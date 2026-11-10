"""D 方案：OCR 降级发送（截图 → 识别 → 点击输入框 / 发送按钮）。

这是 UIA 发送失败时的降级路径，不是首选。公开反馈里一条消息经常要等十余秒，
定位还受分辨率、窗口位置和布局变化影响。真实 Windows 驱动与 OCR 引擎只提供
延迟导入骨架，尚未在真实微信 4.x 上验证；Linux / box 请使用 Fake 实现。

强制要求：
- 走现有限速层（可注入 RateLimiter；生产路径必须经 HumanizedSender）。
- 公开 send_text 在已挂接 HumanizedSender 时走同一套限速、运行模式与 PAUSE；
  官方出口以 through_official=True 进入实际操作，避免二次扣令牌。
- 未挂接正式发送器、也未注入 SendGate 时默认拒发（未知运行模式等同 observe），
  仅允许显式测试开关 allow_ungated_for_tests 放行。
- 发送前有输入停顿。
- 发现异常弹窗 / 验证码立即写入 data/PAUSE 并停止，不得打码或绕过。
- 遵守与 UIA 发送相同的紧急暂停约定（见 src.adapters.pause）。
- 同一群同一文案在窗口内不重复发送。
- 群名规范化后严格精确匹配；适配器内每小时上限默认 40 条，即使未注入限速器也生效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import sys
import threading
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
from collections.abc import Iterable
from typing import Any

from src.adapters.base import OutboundAdapter, SentMessage
from src.adapters.pause import PauseGate, SendPausedError
from src.clock import Clock, SystemClock
from src.logging_util import redact
from src.ratelimit.bucket import RateLimiter


logger = logging.getLogger("wechat_bot")

# 与核查契约 tests/constants.py 中 OCR_CONFIDENCE_THRESHOLD 保持一致。
DEFAULT_OCR_THRESHOLD = 0.70

# 发现这些文字时视为异常弹窗 / 验证码，立即暂停。绝不尝试自动处理。
DEFAULT_DIALOG_LABELS = (
    "验证码",
    "安全验证",
    "异常登录",
    "环境异常",
    "请完成验证",
    "滑块",
    "账号异常",
)

DEFAULT_INPUT_LABELS = ("输入", "发送消息", "请输入")
DEFAULT_SEND_LABELS = ("发送", "Send")

# 直接调用 send_text 时仍生效的每小时上限（与产品默认 40 条一致）。
DEFAULT_REPLIES_PER_HOUR = 40


def normalize_ui_text(text: str) -> str:
    """规范化 UI 文本后再做精确比较：兼容字符、全半角、空白、大小写。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(ch for ch in folded if not ch.isspace())


def texts_match_exactly(left: str, right: str) -> bool:
    """规范化后完全相等才算匹配，禁止子串互相包含。"""
    a = normalize_ui_text(left)
    b = normalize_ui_text(right)
    return bool(a) and a == b


def box_center(box: "TextBox") -> "ScreenPoint":
    return ScreenPoint(
        x=box.x + box.width // 2,
        y=box.y + box.height // 2,
        text=box.text,
        confidence=box.confidence,
    )




class RateLimitedError(RuntimeError):
    """限速未通过，本轮不发送。"""


class GroupNotConfirmedError(RuntimeError):
    """未能在屏幕上确认目标群名，为避免发错会话而中止。"""


class CaptchaOrDialogError(SendPausedError):
    """检测到验证码或异常弹窗，已写入暂停文件。"""


class DependencyMissingError(RuntimeError):
    """真实驱动或 OCR 引擎缺少可选依赖。"""


# ---------------------------------------------------------------------------
# OCR 识别结果与引擎
# ---------------------------------------------------------------------------


@dataclass
class TextBox:
    """屏幕上的一处文字位置。"""

    text: str
    x: int
    y: int
    width: int = 0
    height: int = 0
    confidence: float = 0.0


@dataclass
class OcrResult:
    """一次识别结果。低置信度、空文本或出错时打上降级标记。"""

    text: str
    confidence: float
    fallback_used: bool = False
    flags: list[str] = field(default_factory=list)
    error: str = ""
    boxes: list[TextBox] = field(default_factory=list)


class OcrEngine(ABC):
    """把截图识别成文字与位置。"""

    @abstractmethod
    def recognize(self, image: Any = None) -> OcrResult:
        """识别 image。image 可以是字节、路径或驱动约定的截图对象。"""


class FakeOcrEngine(OcrEngine):
    """按脚本返回结果，供 box 测试。不调用真实 OCR。"""

    def __init__(
        self,
        scripted: list[Any] | None = None,
        *,
        default: OcrResult | None = None,
    ) -> None:
        self.scripted = list(scripted or [])
        self.default = default
        self._idx = 0

    def recognize(self, image: Any = None) -> OcrResult:
        if self._idx < len(self.scripted):
            raw = self.scripted[self._idx]
            self._idx += 1
            return _coerce_ocr_result(raw)
        if self.default is not None:
            return _coerce_ocr_result(self.default)
        return OcrResult(text="", confidence=0.0, error="no_scripted_result")

    def reset(self) -> None:
        self._idx = 0


def _coerce_ocr_result(raw: Any) -> OcrResult:
    if isinstance(raw, OcrResult):
        return raw
    if not isinstance(raw, dict):
        raise TypeError(f"无法把 {type(raw)!r} 转为 OcrResult")
    boxes: list[TextBox] = []
    for item in raw.get("boxes") or []:
        if isinstance(item, TextBox):
            boxes.append(item)
        else:
            boxes.append(
                TextBox(
                    text=str(item.get("text") or ""),
                    x=int(item.get("x") or 0),
                    y=int(item.get("y") or 0),
                    width=int(item.get("width") or 0),
                    height=int(item.get("height") or 0),
                    confidence=float(item.get("confidence") or 0.0),
                )
            )
    return OcrResult(
        text=str(raw.get("text") or ""),
        confidence=float(raw.get("confidence") or 0.0),
        error=str(raw.get("error") or ""),
        boxes=boxes,
        flags=list(raw.get("flags") or []),
        fallback_used=bool(raw.get("fallback_used") or raw.get("degraded") or False),
    )


class OcrFallback:
    """按置信度决定是否降级，并在结果上打标记。契约测试会发现本类。"""

    def __init__(
        self,
        scripted: list[Any] | None = None,
        threshold: float = DEFAULT_OCR_THRESHOLD,
        engine: OcrEngine | None = None,
    ) -> None:
        self.threshold = float(threshold)
        self.engine = engine or FakeOcrEngine(scripted=scripted)
        if scripted is not None and isinstance(self.engine, FakeOcrEngine) and not self.engine.scripted:
            self.engine.scripted = list(scripted)

    def should_fallback(self, result: OcrResult | dict | None) -> bool:
        if result is None:
            return True
        if isinstance(result, dict):
            conf = float(result.get("confidence") or 0.0)
            text = result.get("text") or ""
            error = result.get("error") or ""
        else:
            conf = float(result.confidence)
            text = result.text or ""
            error = result.error or ""
        if error:
            return True
        if not str(text).strip():
            return True
        return conf < self.threshold

    def recognize(self, image: Any = None) -> OcrResult:
        result = self.engine.recognize(image)
        if self.should_fallback(result):
            result.fallback_used = True
            if "ocr_degraded" not in result.flags:
                result.flags.append("ocr_degraded")
        return result


class TesseractOcrEngine(OcrEngine):
    """pytesseract 骨架。延迟导入，不假装已经验证。"""

    def __init__(self) -> None:
        self._ensure_deps()

    def _ensure_deps(self) -> None:
        try:
            import pytesseract  # noqa: F401
        except ImportError as exc:
            raise DependencyMissingError(
                "TesseractOcrEngine 需要 pytesseract，并在系统中安装 Tesseract。"
                "请在 Windows 上执行: pip install pytesseract pillow。"
                "本骨架尚未在真实微信 4.x 上验证，安装依赖不等于已经可用。"
            ) from exc

    def recognize(self, image: Any = None) -> OcrResult:
        raise NotImplementedError(
            "Tesseract OCR 仍是未验证骨架，不会在未验证时假装识别成功。"
            "box / 测试请使用 FakeOcrEngine 或 OcrFallback(scripted=...)。"
        )


class RapidOcrEngine(OcrEngine):
    """RapidOCR 骨架。延迟导入，不假装已经验证。"""

    def __init__(self) -> None:
        self._ensure_deps()

    def _ensure_deps(self) -> None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError as exc:
            try:
                import rapidocr  # noqa: F401
            except ImportError as exc2:
                raise DependencyMissingError(
                    "RapidOcrEngine 需要 rapidocr_onnxruntime 或 rapidocr。"
                    "请在 Windows 上执行: pip install rapidocr-onnxruntime。"
                    "本骨架尚未在真实微信 4.x 上验证，安装依赖不等于已经可用。"
                ) from exc2

    def recognize(self, image: Any = None) -> OcrResult:
        raise NotImplementedError(
            "RapidOCR 仍是未验证骨架，不会在未验证时假装识别成功。"
            "box / 测试请使用 FakeOcrEngine 或 OcrFallback(scripted=...)。"
        )


# ---------------------------------------------------------------------------
# 屏幕驱动
# ---------------------------------------------------------------------------


@dataclass
class ScreenPoint:
    x: int
    y: int
    text: str = ""
    confidence: float = 0.0


class ScreenDriver(ABC):
    """截图、点击、粘贴与按文字定位的抽象。"""

    @abstractmethod
    def screenshot(self, region: tuple[int, int, int, int] | None = None) -> Any:
        """截取全屏或指定区域，返回引擎可识别的图像对象。"""

    @abstractmethod
    def click(self, x: int, y: int) -> None:
        """点击屏幕坐标。"""

    @abstractmethod
    def paste(self, text: str) -> None:
        """把文本粘贴到当前焦点（不逐字模拟键盘轰炸）。"""

    @abstractmethod
    def locate_text(
        self,
        text: str,
        image: Any = None,
        *,
        min_confidence: float = DEFAULT_OCR_THRESHOLD,
    ) -> ScreenPoint | None:
        """在截图中定位包含 text 的位置，找不到返回 None。"""


class FakeScreenDriver(ScreenDriver):
    """记录操作的假驱动，可注入屏幕上的文字框与弹窗。"""

    def __init__(
        self,
        elements: list[dict[str, Any] | TextBox] | None = None,
        *,
        dialogs: list[str] | None = None,
        image: Any = b"fake-screenshot",
    ) -> None:
        self.elements: list[TextBox] = []
        for item in elements or []:
            if isinstance(item, TextBox):
                self.elements.append(item)
            else:
                self.elements.append(
                    TextBox(
                        text=str(item.get("text") or ""),
                        x=int(item.get("x") or 0),
                        y=int(item.get("y") or 0),
                        width=int(item.get("width") or 0),
                        height=int(item.get("height") or 0),
                        confidence=float(item.get("confidence") or 1.0),
                    )
                )
        self.dialogs = list(dialogs or [])
        self.image = image
        self.clicks: list[tuple[int, int]] = []
        self.pastes: list[str] = []
        self.screenshots: list[tuple[int, int, int, int] | None] = []

    def screenshot(self, region: tuple[int, int, int, int] | None = None) -> Any:
        self.screenshots.append(region)
        return self.image

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def paste(self, text: str) -> None:
        self.pastes.append(text)

    def locate_text(
        self,
        text: str,
        image: Any = None,
        *,
        min_confidence: float = DEFAULT_OCR_THRESHOLD,
    ) -> ScreenPoint | None:
        matches = self.locate_all_exact(text, min_confidence=min_confidence)
        if not matches:
            return None
        return matches[0]

    def locate_all_exact(
        self,
        text: str,
        *,
        min_confidence: float = DEFAULT_OCR_THRESHOLD,
    ) -> list[ScreenPoint]:
        """返回所有规范化后精确相等的位置。"""
        needle = normalize_ui_text(text)
        if not needle:
            return []
        hits: list[ScreenPoint] = []
        for box in self.elements:
            if box.confidence < min_confidence:
                continue
            if normalize_ui_text(box.text) == needle:
                hits.append(box_center(box))
        return hits

    def visible_text(self) -> str:
        parts = [box.text for box in self.elements]
        parts.extend(self.dialogs)
        return "\n".join(parts)


class WindowsScreenDriver(ScreenDriver):
    """pyautogui 屏幕驱动骨架。延迟导入，Linux 上 import 本模块不会失败。"""

    def __init__(self, engine: OcrEngine | None = None) -> None:
        self.engine = engine
        self._ensure_platform()
        self._ensure_deps()

    def _ensure_platform(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsScreenDriver 只能在 Windows 上使用，"
                f"当前平台是 {sys.platform}。"
                "box / Linux 请使用 FakeScreenDriver。"
                "本骨架尚未在真实微信上验证。"
            )

    def _ensure_deps(self) -> None:
        try:
            import pyautogui  # noqa: F401
            import PIL  # noqa: F401
        except ImportError as exc:
            raise DependencyMissingError(
                "WindowsScreenDriver 需要 pyautogui 与 pillow。"
                "请在 Windows 上执行: pip install pyautogui pillow。"
                "本骨架尚未在真实微信 4.x 上验证，安装依赖不等于已经可用。"
            ) from exc

    def screenshot(self, region: tuple[int, int, int, int] | None = None) -> Any:
        raise NotImplementedError(self._unverified("截图"))

    def click(self, x: int, y: int) -> None:
        raise NotImplementedError(self._unverified("点击"))

    def paste(self, text: str) -> None:
        raise NotImplementedError(self._unverified("粘贴"))

    def locate_text(
        self,
        text: str,
        image: Any = None,
        *,
        min_confidence: float = DEFAULT_OCR_THRESHOLD,
    ) -> ScreenPoint | None:
        raise NotImplementedError(self._unverified("文字定位"))

    @staticmethod
    def _unverified(action: str) -> str:
        return (
            f"WindowsScreenDriver.{action} 仍是未验证骨架，"
            "不会在未验证时假装已经操作了微信窗口。"
            "box / 测试请使用 FakeScreenDriver。"
        )


# ---------------------------------------------------------------------------
# 幂等记录
# ---------------------------------------------------------------------------


class SendIdempotencyLog:
    """同一群同一文案在时间窗内不重复发送，重启后仍生效。"""

    def __init__(
        self,
        path: str | Path,
        *,
        window_seconds: float = 600.0,
        clock: Clock | None = None,
    ) -> None:
        self.path = Path(path)
        self.window_seconds = float(window_seconds)
        self._clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._entries: dict[str, str] = {}
        self._load()

    @staticmethod
    def make_key(group_id: str, text: str) -> str:
        payload = f"{group_id}\n{text}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def already_sent(self, group_id: str, text: str) -> bool:
        key = self.make_key(group_id, text)
        now = self._clock.now()
        with self._lock:
            self._purge(now)
            raw = self._entries.get(key)
            if not raw:
                return False
            try:
                sent_at = datetime.fromisoformat(raw)
            except ValueError:
                return False
            return (now - sent_at) <= timedelta(seconds=self.window_seconds)

    def record(self, group_id: str, text: str) -> None:
        key = self.make_key(group_id, text)
        now = self._clock.now()
        with self._lock:
            self._entries[key] = now.isoformat(timespec="seconds")
            self._purge(now)
            self._dump()

    def _purge(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        stale = []
        for key, raw in self._entries.items():
            try:
                sent_at = datetime.fromisoformat(raw)
            except ValueError:
                stale.append(key)
                continue
            if sent_at < cutoff:
                stale.append(key)
        for key in stale:
            self._entries.pop(key, None)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._entries = {}
            return
        if isinstance(data, dict):
            self._entries = {str(k): str(v) for k, v in data.items()}

    def _dump(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# 发送适配器
# ---------------------------------------------------------------------------


@dataclass
class OcrSendResult:
    """一次 OCR 发送的结果（send_text 本身按接口返回 None，结果放在 history 里）。"""

    ok: bool
    status: str
    group_id: str
    text: str
    error: str = ""
    dry_run: bool = False


class OcrOutboundAdapter(OutboundAdapter):
    """OCR 降级发送适配器（角色 D，outbound=ocr）。"""


    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None = None,
        *,
        clock: Clock | None = None,
        limiter: RateLimiter | None = None,
    ) -> "OcrOutboundAdapter":
        """根据 wechat.ocr 段构造适配器。默认使用 Fake 驱动，便于 box 测试。"""
        root = config or {}
        wechat = root.get("wechat") or {}
        section = {}
        for candidate in (
            wechat.get("ocr") if isinstance(wechat, dict) else None,
            (root.get("outbound") or {}).get("ocr") if isinstance(root.get("outbound"), dict) else None,
        ):
            if isinstance(candidate, dict):
                section.update(candidate)
        bot = root.get("bot") or {}
        data_dir = section.get("data_dir") or bot.get("data_dir") or "data"
        driver_name = str(section.get("driver") or "fake").strip().lower()
        engine_name = str(section.get("engine") or "tesseract").strip().lower()
        group_names: dict[str, str] = {}
        for item in root.get("groups") or []:
            if isinstance(item, dict) and item.get("group_id"):
                group_names[str(item["group_id"])] = str(item.get("name") or item["group_id"])
        extra = section.get("group_names") or {}
        if isinstance(extra, dict):
            group_names.update({str(k): str(v) for k, v in extra.items()})
        if driver_name == "windows":
            driver, engine = build_windows_ocr_stack(engine_name)
        else:
            driver = FakeScreenDriver()
            engine = FakeOcrEngine()
        pause_file = section.get("pause_file") or section.get("pause_flag")
        rate_section = root.get("rate_limit") or {}
        if not isinstance(rate_section, dict):
            rate_section = {}
        per_hour = section.get("global_replies_per_hour")
        if per_hour is None:
            per_hour = rate_section.get("global_replies_per_hour", DEFAULT_REPLIES_PER_HOUR)
        return cls(
            driver=driver,
            engine=engine,
            limiter=limiter,
            enforce_rate_limit=bool(section.get("enforce_rate_limit", False)),
            global_replies_per_hour=int(per_hour),
            allow_ungated_for_tests=bool(section.get("allow_ungated_for_tests", False)),
            pause=PauseGate(path=pause_file, data_dir=data_dir),
            data_dir=data_dir,
            clock=clock,
            dry_run=bool(section.get("dry_run", False)),
            group_names=group_names,
            input_pause_min=float(section.get("input_pause_min", 0.4)),
            input_pause_max=float(section.get("input_pause_max", 1.2)),
            max_rate_wait_seconds=float(section.get("max_rate_wait_seconds", 30.0)),
            idempotency_window_seconds=float(section.get("idempotency_window_seconds", 600.0)),
            confirm_group_name=bool(section.get("confirm_group_name", True)),
        )

    def __init__(
        self,
        driver: ScreenDriver | None = None,
        engine: OcrEngine | None = None,
        *,
        limiter: RateLimiter | None = None,
        enforce_rate_limit: bool = True,
        pause: PauseGate | None = None,
        data_dir: str | Path | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        dry_run: bool = False,
        group_names: dict[str, str] | None = None,
        input_labels: Iterable[str] | None = None,
        send_labels: Iterable[str] | None = None,
        dialog_labels: Iterable[str] | None = None,
        input_pause_min: float = 0.4,
        input_pause_max: float = 1.2,
        max_rate_wait_seconds: float = 30.0,
        idempotency_window_seconds: float = 600.0,
        confirm_group_name: bool = True,
        send_kind: str = "auto_reply",
        global_replies_per_hour: int = DEFAULT_REPLIES_PER_HOUR,
        send_gate: object | None = None,
        allow_ungated_for_tests: bool = False,
    ) -> None:
        self.driver = driver or FakeScreenDriver()
        self.engine = engine or FakeOcrEngine()
        self.limiter = limiter
        self.enforce_rate_limit = bool(enforce_rate_limit)
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self.dry_run = bool(dry_run)
        self.group_names = dict(group_names or {})
        self.input_labels = tuple(input_labels or DEFAULT_INPUT_LABELS)
        self.send_labels = tuple(send_labels or DEFAULT_SEND_LABELS)
        self.dialog_labels = tuple(dialog_labels or DEFAULT_DIALOG_LABELS)
        self.input_pause_min = float(input_pause_min)
        self.input_pause_max = float(input_pause_max)
        self.max_rate_wait_seconds = float(max_rate_wait_seconds)
        self.confirm_group_name = bool(confirm_group_name)
        self.send_kind = send_kind
        self.global_replies_per_hour = int(global_replies_per_hour)
        self._hour_sends: deque[datetime] = deque()
        data_root = Path(data_dir) if data_dir is not None else Path("data")
        self.pause = pause or PauseGate(data_dir=data_root)
        self._idem = SendIdempotencyLog(
            data_root / "ocr_idempotency.json",
            window_seconds=idempotency_window_seconds,
            clock=self.clock,
        )
        self._sent: list[SentMessage] = []
        self.history: list[OcrSendResult] = []
        self._lock = threading.RLock()
        self._official_sender = None
        self.send_gate = send_gate
        self.allow_ungated_for_tests = bool(allow_ungated_for_tests)
        self._last_result: OcrSendResult | None = None

    @property
    def sent_messages(self) -> list[SentMessage]:
        with self._lock:
            return list(self._sent)

    @property
    def last_result(self) -> OcrSendResult | None:
        return self._last_result

    def attach_official_sender(self, sender: object) -> None:
        """由 HumanizedSender 挂接正式出口（限速 + 运行模式 + 暂停）。"""
        self._official_sender = sender

    def send_text(
        self,
        group_id: str,
        text: str,
        *,
        send_id: str | None = None,
        through_official: bool = False,
        kind: str | None = None,
    ) -> None:
        """公开发送入口。未带 through_official 时走挂接的正式闸门。

        无正式发送器且未注入 SendGate 时默认拒发，避免在 observe 约定下绕过运行模式。
        仅当 allow_ungated_for_tests=True 时才允许这种无闸门直发（测试用）。
        """
        result = self._send(
            group_id,
            text,
            send_id=send_id,
            through_official=through_official,
            kind=kind or self.send_kind,
        )
        self._last_result = result
        self.history.append(result)
        if through_official:
            # 正式出口以 last_result 回报成败，避免异常冲出 HumanizedSender.pump。
            return
        if not result.ok and result.status not in {"skipped_duplicate", "dry_run"}:
            # dry_run / 幂等跳过不算失败异常，其它失败向上抛，便于调用方感知。
            if result.status == "paused":
                raise SendPausedError(result.error)
            if result.status == "captcha_or_dialog":
                raise CaptchaOrDialogError(result.error)
            if result.status == "rate_limited":
                raise RateLimitedError(result.error)
            if result.status == "group_not_confirmed":
                raise GroupNotConfirmedError(result.error)
            if result.error:
                raise RuntimeError(result.error)

    def _send(
        self,
        group_id: str,
        text: str,
        *,
        send_id: str | None = None,
        through_official: bool = False,
        kind: str | None = None,
    ) -> OcrSendResult:
        send_kind = kind or self.send_kind
        if not through_official:
            blocked = self._blocked_by_gate(send_kind, group_id)
            if blocked is not None:
                status, error = blocked
                return OcrSendResult(False, status, group_id, text, error)

        try:
            self.pause.check()
        except SendPausedError as exc:
            return OcrSendResult(False, "paused", group_id, text, str(exc))

        if self._idem.already_sent(group_id, text):
            return OcrSendResult(
                True,
                "skipped_duplicate",
                group_id,
                text,
                "同一群同一文案在幂等窗口内已发送过，跳过以避免重复。",
            )

        if self._hourly_limit_reached():
            return OcrSendResult(
                False,
                "rate_limited",
                group_id,
                text,
                f"已达到每小时 {self.global_replies_per_hour} 条上限，本轮不发送。",
            )

        # 已经由 HumanizedSender / 正式闸门扣过令牌时不再 acquire，避免二次扣除。
        use_own_limiter = (
            not through_official
            and self._official_sender is None
            and self.limiter is not None
            and self.enforce_rate_limit
        )
        if use_own_limiter:
            now = self.clock.now()
            wait = self.limiter.time_until_allow(group_id, send_kind, now)
            if wait > self.max_rate_wait_seconds:
                return OcrSendResult(
                    False,
                    "rate_limited",
                    group_id,
                    text,
                    f"限速等待 {wait:.1f}s 超过上限 {self.max_rate_wait_seconds:.1f}s",
                )
            if wait > 0:
                self.clock.sleep(wait)
            if not self.limiter.try_acquire(group_id, send_kind, self.clock.now()):
                return OcrSendResult(
                    False,
                    "rate_limited",
                    group_id,
                    text,
                    "限速令牌不足，本轮不发送。",
                )

        # 输入停顿：即使走了 HumanizedSender，OCR 路径仍额外停一下，模拟找框与落键。
        pause = self._input_pause()
        if pause > 0:
            self.clock.sleep(pause)

        # 再检查一次暂停，避免停顿期间被人打了 PAUSE 或弹出验证码。
        try:
            self.pause.check()
        except SendPausedError as exc:
            return OcrSendResult(False, "paused", group_id, text, str(exc))

        image = self.driver.screenshot()
        ocr = self.engine.recognize(image)
        visible = (ocr.text or "") + "\n" + self._driver_visible_text()
        hit = self._find_dialog(visible, ocr)
        if hit:
            reason = f"检测到异常弹窗或验证码（「{hit}」），已紧急暂停，等待人工处理。"
            self.pause.pause(reason)
            return OcrSendResult(False, "captcha_or_dialog", group_id, text, reason)

        if self.confirm_group_name:
            confirm_error, unique = self._confirm_group_name(group_id, image, ocr)
            if confirm_error:
                status = "group_not_confirmed"
                return OcrSendResult(False, status, group_id, text, confirm_error)

        input_pt = self._locate_any(self.input_labels, image)
        send_pt = self._locate_any(self.send_labels, image)
        if input_pt is None or send_pt is None:
            return OcrSendResult(
                False,
                "ui_not_located",
                group_id,
                text,
                "未能定位输入框或发送按钮（OCR 降级路径定位失败）。",
            )

        if self.dry_run:
            record = SentMessage(
                group_id=group_id,
                text=text,
                timestamp=self.clock.now(),
                extra={"dry_run": True, "adapter": "ocr"},
            )
            with self._lock:
                self._sent.append(record)
            self._record_hourly()
            return OcrSendResult(True, "dry_run", group_id, text, dry_run=True)

        self.driver.click(input_pt.x, input_pt.y)
        self.driver.paste(text)
        # 粘贴后再停一小段再点发送。
        confirm_pause = min(self.input_pause_min, 0.4)
        if confirm_pause > 0:
            self.clock.sleep(confirm_pause)
        self.pause.check()
        self.driver.click(send_pt.x, send_pt.y)
        self._idem.record(group_id, text)
        record = SentMessage(
            group_id=group_id,
            text=text,
            timestamp=self.clock.now(),
            extra={"adapter": "ocr"},
        )
        with self._lock:
            self._sent.append(record)
        self._record_hourly()
        return OcrSendResult(True, "ok", group_id, text)

    def _log_gate_drop(self, reason: str, *, kind: str, group_id: str) -> None:
        """记录被闸门丢弃的发送。不写入消息正文，便于脱敏日志过滤器处理。"""
        logger.info(
            "发送已丢弃：%s；kind=%s group_id=%s adapter=ocr",
            redact(reason),
            redact(kind),
            redact(group_id or "-"),
        )

    def _decision_to_block(self, decision: object, *, kind: str, group_id: str) -> tuple[str, str] | None:
        if decision is None or getattr(decision, "allowed", True):
            return None
        reason = str(getattr(decision, "reason", "") or "发送闸门拒绝")
        status = "paused" if getattr(decision, "paused", False) else "rejected"
        self._log_gate_drop(reason, kind=kind, group_id=group_id)
        return status, reason

    def _blocked_by_official_gate(self, kind: str, group_id: str) -> tuple[str, str] | None:
        """已挂接正式发送器时，直接 send_text 也必须经过运行模式、暂停与限速。"""
        sender = self._official_sender
        if sender is None:
            return None
        gate = getattr(sender, "send_gate", None) or self.send_gate
        if gate is not None and hasattr(gate, "decide"):
            blocked = self._decision_to_block(gate.decide(kind), kind=kind, group_id=group_id)
            if blocked is not None:
                return blocked
        limiter = getattr(sender, "limiter", None)
        if limiter is not None and hasattr(limiter, "try_acquire"):
            kind_name = kind if kind in {"auto_reply", "announcement"} else "auto_reply"
            if not limiter.try_acquire(group_id, kind_name, self.clock.now()):
                reason = "已达到限速配额，直接发送被拒绝"
                self._log_gate_drop(reason, kind=kind_name, group_id=group_id)
                return "rate_limited", reason
        return None

    def _blocked_by_gate(self, kind: str, group_id: str) -> tuple[str, str] | None:
        if self._official_sender is not None:
            return self._blocked_by_official_gate(kind, group_id)
        if self.send_gate is not None and hasattr(self.send_gate, "decide"):
            return self._decision_to_block(self.send_gate.decide(kind), kind=kind, group_id=group_id)
        if self.allow_ungated_for_tests:
            return None
        reason = (
            "未挂接正式发送闸门，默认拒绝发送"
            "（运行模式未知，等同 observe）。请经 HumanizedSender 发送或注入 SendGate。"
        )
        self._log_gate_drop(reason, kind=kind, group_id=group_id)
        return "rejected", reason

    def _hourly_limit_reached(self) -> bool:
        """适配器内统一限额：即使未注入 RateLimiter / 关闭 enforce_rate_limit 也生效。"""
        limit = self.global_replies_per_hour
        if limit <= 0:
            return False
        now = self.clock.now()
        cutoff = now - timedelta(hours=1)
        with self._lock:
            while self._hour_sends and self._hour_sends[0] <= cutoff:
                self._hour_sends.popleft()
            return len(self._hour_sends) >= limit

    def _record_hourly(self) -> None:
        if self.global_replies_per_hour <= 0:
            return
        with self._lock:
            self._hour_sends.append(self.clock.now())

    def _iter_screen_boxes(self, ocr: OcrResult | None = None) -> list[TextBox]:
        boxes: list[TextBox] = []
        if hasattr(self.driver, "elements"):
            boxes.extend(list(getattr(self.driver, "elements") or []))
        if ocr is not None:
            boxes.extend(list(ocr.boxes or []))
        return boxes

    def _exact_group_matches(
        self,
        label: str,
        image: Any,
        ocr: OcrResult | None = None,
    ) -> list[ScreenPoint]:
        if hasattr(self.driver, "locate_all_exact"):
            found = list(self.driver.locate_all_exact(label))
            if found:
                return found
        needle = normalize_ui_text(label)
        if not needle:
            return []
        hits: list[ScreenPoint] = []
        seen: set[tuple[int, int, str]] = set()
        for box in self._iter_screen_boxes(ocr):
            if box.confidence < DEFAULT_OCR_THRESHOLD:
                continue
            if normalize_ui_text(box.text) != needle:
                continue
            point = box_center(box)
            key = (point.x, point.y, point.text)
            if key in seen:
                continue
            seen.add(key)
            hits.append(point)
        return hits

    def _confirm_group_name(
        self,
        group_id: str,
        image: Any,
        ocr: OcrResult | None = None,
    ) -> tuple[str | None, bool]:
        """规范化后精确匹配当前群名。

        返回 (错误信息, 是否唯一命中)。无匹配或匹配不唯一时不发送；
        不唯一或不确定时写入 PAUSE。
        """
        display = self.group_names.get(group_id, group_id)
        if not normalize_ui_text(display):
            return "目标群名为空，无法确认当前会话，已中止。", False
        matches = self._exact_group_matches(display, image, ocr)
        if len(matches) == 1:
            return None, True
        if len(matches) > 1:
            reason = (
                f"屏幕上群名「{display}」匹配不唯一（{len(matches)} 处），"
                "已紧急暂停，避免发错会话。"
            )
            self.pause.pause(reason)
            return reason, False
        return (
            f"未能在屏幕上确认群名「{display}」，为避免发错会话已中止。",
            False,
        )

    def _input_pause(self) -> float:
        lo = max(0.0, self.input_pause_min)
        hi = max(lo, self.input_pause_max)
        if hi <= lo:
            return lo
        return float(self.rng.uniform(lo, hi))

    def _driver_visible_text(self) -> str:
        if hasattr(self.driver, "visible_text"):
            return str(self.driver.visible_text())
        return ""

    def _find_dialog(self, visible: str, result: OcrResult) -> str | None:
        blobs = [visible]
        blobs.extend(box.text for box in result.boxes)
        haystack = "\n".join(blobs)
        for label in self.dialog_labels:
            if label and label in haystack:
                return label
        return None

    def _locate_any(self, labels: Iterable[str], image: Any) -> ScreenPoint | None:
        for label in labels:
            if not label:
                continue
            found = self.driver.locate_text(label, image)
            if found is not None:
                return found
        # 驱动定位不到时，退回到本次 OCR 结果中的框。
        if hasattr(self.engine, "recognize") and image is not None:
            # 避免再次调用会消耗 FakeOcrEngine 脚本；只在 driver 元素里找过即可。
            pass
        return None


def build_windows_ocr_stack(engine_name: str = "tesseract") -> tuple[ScreenDriver, OcrEngine]:
    """构建真实 Windows 驱动与引擎。未验证，缺依赖或非 Windows 会清晰报错。"""
    name = (engine_name or "tesseract").strip().lower()
    if name in {"rapid", "rapidocr"}:
        engine: OcrEngine = RapidOcrEngine()
    elif name in {"tesseract", "pytesseract"}:
        engine = TesseractOcrEngine()
    else:
        raise ValueError(f"未知 OCR 引擎: {engine_name}（可选 tesseract / rapidocr）")
    return WindowsScreenDriver(engine=engine), engine


# 契约发现可能扫到的别名。recognize 只放在 OcrFallback / 引擎上，避免误匹配发送器。
OcrAdapter = OcrFallback

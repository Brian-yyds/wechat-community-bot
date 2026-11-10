"""适配器 B：官方 PC 微信客户端 UI Automation 发送（OutboundAdapter）。

真实发送只能在用户 Windows 电脑上进行；box 上使用 FakeUiDriver 模拟。
发送路径必须由上层 HumanizedSender / RateLimiter 调用，本适配器不提供绕过限频的入口。
适配器自身还会叠加：输入停顿、群切换间隔、单次发送最小间隔，以及紧急暂停。

延迟目标（仅计「适配器自身耗时」，不含限速层 2–6 秒拟人延迟）：
- 已打开会话：规则回复发出 ≤ 3 秒
- 未打开会话：≤ 8 秒

幂等：同一待发消息 send_id 只成功发出一次；按下发送后若校验不确定，标记 maybe_sent，不再重试以免刷屏。
出现锁屏 / 窗口不可见 / 掉线 / 验证码或异常弹窗时立即暂停并告警，不自动反复重登。

严禁博彩、六合彩、下注、结算、收款及相关功能。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.adapters.base import OutboundAdapter, SentMessage
from src.adapters.pause import PAUSE_ENV_VAR, PauseGate, resolve_pause_path
from src.adapters.ui_driver import (
    ChatNotFoundError,
    FakeUiDriver,
    SendTimeoutError,
    UiaError,
    UiDriver,
    UiNotReadyError,
    titles_match,
)
from src.clock import Clock, SystemClock
from src.logging_util import redact

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, dict[str, Any]], None]


class UiaSendStatus:
    """与契约测试约定一致的状态码。"""

    OK = "ok"
    TIMEOUT = "timeout"
    TARGET_NOT_FOUND = "target_not_found"
    UI_NOT_READY = "ui_not_ready"
    PAUSED = "paused"
    MAYBE_SENT = "maybe_sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass
class StageTimings:
    """发送路径各段耗时（秒）。adapter_total 不含限速层拟人延迟。"""

    open_chat: float = 0.0
    paste: float = 0.0
    send: float = 0.0
    verify: float = 0.0
    preheat: float = 0.0
    input_pause: float = 0.0
    group_switch_wait: float = 0.0
    safety: float = 0.0
    adapter_total: float = 0.0
    chat_was_open: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 保留毫秒级可读性
        for key, value in list(data.items()):
            if isinstance(value, float):
                data[key] = round(value, 4)
        return data


@dataclass
class UiaSendResult:
    """一次 UIA 发送的结果，供调用方与自检脚本读取分段耗时。"""

    ok: bool
    status: str
    group: str
    text: str
    error: str = ""
    elapsed: float = 0.0
    timings: StageTimings = field(default_factory=StageTimings)
    send_id: str | None = None
    skipped: bool = False
    attempts: int = 0

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class UiaConfig:
    """outbound.uia 配置。时间单位均为秒。"""

    group_names: dict[str, str] = field(default_factory=dict)
    pause_flag: str = "data/PAUSE"
    data_dir: str = "data"
    max_retries: int = 2
    send_timeout_seconds: float = 8.0
    # 适配器内拟人（计入自身耗时，须给已打开会话留下 ≤3s 预算）
    input_pause_min: float = 0.12
    input_pause_max: float = 0.35
    group_switch_interval_min: float = 0.35
    group_switch_interval_max: float = 0.70
    min_send_interval_seconds: float = 0.80
    # 会话预热 / 保持目标群已打开
    keep_chat_open: bool = True
    preheat_on_start: bool = True
    preheat_groups: list[str] = field(default_factory=list)
    return_to_primary_after_send: bool = True
    reopen_min_interval_seconds: float = 30.0
    primary_group: str | None = None
    # 延迟目标（文档与自检对照，不在运行时强制失败）
    target_open_chat_seconds: float = 3.0
    target_unopened_chat_seconds: float = 8.0
    # 非 Windows 时是否允许自动改用 FakeUiDriver（box 测试）
    allow_fake_on_non_windows: bool = True

    @classmethod
    def from_mapping(
        cls,
        config: dict[str, Any] | None,
        *,
        project_root: Path | None = None,
    ) -> UiaConfig:
        root = project_root or Path(".")
        raw_root = config or {}
        # 兼容 [outbound.uia] 与 [wechat.uia] / wechat.outbound_uia
        section = (
            ((raw_root.get("outbound") or {}).get("uia") if isinstance(raw_root.get("outbound"), dict) else None)
            or ((raw_root.get("wechat") or {}).get("uia") if isinstance(raw_root.get("wechat"), dict) else None)
            or raw_root.get("uia")
            or {}
        )
        if not isinstance(section, dict):
            section = {}
        bot = raw_root.get("bot") or {}
        data_dir = str(section.get("data_dir") or bot.get("data_dir") or "data")
        groups_map: dict[str, str] = {}
        for item in raw_root.get("groups") or []:
            if isinstance(item, dict) and item.get("group_id") and item.get("name"):
                groups_map[str(item["group_id"])] = str(item["name"])
        extra = section.get("group_names") or {}
        if isinstance(extra, dict):
            groups_map.update({str(k): str(v) for k, v in extra.items()})
        preheat = section.get("preheat_groups") or []
        if isinstance(preheat, str):
            preheat = [preheat]
        pause_flag = str(section.get("pause_flag") or "data/PAUSE")
        return cls(
            group_names=groups_map,
            pause_flag=pause_flag,
            data_dir=data_dir,
            max_retries=int(section.get("max_retries", 2)),
            send_timeout_seconds=float(section.get("send_timeout_seconds", 8.0)),
            input_pause_min=float(section.get("input_pause_min", 0.12)),
            input_pause_max=float(section.get("input_pause_max", 0.35)),
            group_switch_interval_min=float(section.get("group_switch_interval_min", 0.35)),
            group_switch_interval_max=float(section.get("group_switch_interval_max", 0.70)),
            min_send_interval_seconds=float(section.get("min_send_interval_seconds", 0.80)),
            keep_chat_open=bool(section.get("keep_chat_open", True)),
            preheat_on_start=bool(section.get("preheat_on_start", True)),
            preheat_groups=[str(x) for x in preheat],
            return_to_primary_after_send=bool(section.get("return_to_primary_after_send", True)),
            reopen_min_interval_seconds=float(section.get("reopen_min_interval_seconds", 30.0)),
            primary_group=(str(section["primary_group"]) if section.get("primary_group") else None),
            target_open_chat_seconds=float(section.get("target_open_chat_seconds", 3.0)),
            target_unopened_chat_seconds=float(section.get("target_unopened_chat_seconds", 8.0)),
            allow_fake_on_non_windows=bool(section.get("allow_fake_on_non_windows", True)),
        )

    def resolve_pause_flag(self, project_root: Path | None = None) -> Path:
        # 显式路径优先；仅当仍是默认 data/PAUSE 时，才走环境变量 / data_dir 约定。
        if self.pause_flag == "data/PAUSE":
            return resolve_pause_path(data_dir=self.resolve_data_dir(project_root))
        path = Path(self.pause_flag)
        if not path.is_absolute():
            path = (project_root or Path(".")) / path
        return resolve_pause_path(path)

    def resolve_data_dir(self, project_root: Path | None = None) -> Path:
        path = Path(self.data_dir)
        if not path.is_absolute():
            path = (project_root or Path(".")) / path
        return path


class SentIdStore:
    """持久化已发送 / 可能已发送的消息 id，避免重启后重复发送。

    文件损坏时进入 fail-safe：隔离旧文件、拒绝读取空记录继续发送，必须人工对账。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self.corrupt = False
        self.corrupt_reason = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise json.JSONDecodeError("幂等记录不是对象", "", 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.corrupt = True
            self.corrupt_reason = str(exc)
            try:
                quarantined = self.path.with_name(self.path.name + ".corrupt")
                self.path.replace(quarantined)
                logger.warning(
                    "读取发送幂等记录失败，已隔离损坏文件并拒绝自动重发: %s",
                    redact(str(exc)),
                )
            except OSError as move_exc:
                logger.warning(
                    "读取发送幂等记录失败且无法隔离文件，拒绝自动重发: %s / %s",
                    redact(str(exc)),
                    redact(str(move_exc)),
                )
            return
        self._data = raw

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, send_id: str) -> str | None:
        with self._lock:
            item = self._data.get(send_id)
            if not item:
                return None
            return str(item.get("status") or "")

    def mark(self, send_id: str, status: str) -> None:
        with self._lock:
            self._data[send_id] = {
                "status": status,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
            self._flush()


def resolve_group_name(group_id: str, mapping: dict[str, str]) -> str:
    """group_id → 微信窗口显示名；未配置时退回原值（可能本身就是显示名）。"""
    return mapping.get(group_id, group_id)


class UiaOutboundAdapter(OutboundAdapter):
    """OutboundAdapter 实现：把文本经 UiDriver 发到指定群。

    公开 send / send_text 在已挂接 HumanizedSender 时必须经过限速、运行模式与 PAUSE；
    未挂接时只做暂停与 UI 防护，不得当成已经限速。HumanizedSender 完成闸门判定后
    以 through_official=True 进入实际 UI 操作。本类同时负责间隔、幂等、暂停与分段打点。
    """

    def __init__(
        self,
        driver: UiDriver | None = None,
        config: UiaConfig | None = None,
        clock: Clock | None = None,
        *,
        project_root: Path | None = None,
        rng: random.Random | None = None,
        on_alert: AlertCallback | None = None,
        fail_mode: str | None = None,
    ) -> None:
        self.project_root = Path(project_root) if project_root else Path(".")
        self.config = config or UiaConfig()
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self._on_alert = on_alert
        if driver is None:
            driver = FakeUiDriver(fail_mode=fail_mode)
        elif fail_mode and isinstance(driver, FakeUiDriver) and driver.fail_mode is None:
            driver.fail_mode = fail_mode
            if fail_mode == "ui_not_ready":
                driver.window_visible = False
        self.driver = driver
        self._lock = threading.RLock()
        self._started = False
        self._emergency_paused = False
        self._pause_reason = ""
        self._last_send_at: datetime | None = None
        self._last_switch_at: datetime | None = None
        self._last_reopen_at: datetime | None = None
        self._last_result: UiaSendResult | None = None
        self._sent_log: list[SentMessage] = []
        self._alerts: list[dict[str, Any]] = []
        self._preheat_timings = 0.0
        data_dir = self.config.resolve_data_dir(self.project_root)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._store = SentIdStore(data_dir / "uia_sent_ids.json")
        self._pause_gate = PauseGate(self.config.resolve_pause_flag(self.project_root))
        self._official_sender = None

    # --- 供测试与自检查询 ---

    @property
    def last_result(self) -> UiaSendResult | None:
        return self._last_result

    @property
    def emergency_paused(self) -> bool:
        return self._emergency_paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    @property
    def alerts(self) -> list[dict[str, Any]]:
        return list(self._alerts)

    @property
    def sent_messages(self) -> list[SentMessage]:
        with self._lock:
            return list(self._sent_log)

    def is_paused(self) -> bool:
        return self._emergency_paused or self._pause_flag_exists()

    def clear_emergency_pause(self) -> None:
        """人工恢复（不自动调用）：删除暂停文件并清除内存标记。"""
        with self._lock:
            self._emergency_paused = False
            self._pause_reason = ""
        self._pause_gate.resume()

    # --- OutboundAdapter ---

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        if self.config.preheat_on_start:
            self.preheat()

    def stop(self) -> None:
        with self._lock:
            self._started = False
        try:
            self.driver.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 UiDriver 时忽略异常", exc_info=True)

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
        kind: str = "auto_reply",
    ) -> None:
        """公开发送入口。未带 through_official 时走挂接的正式闸门。"""
        self.send(
            group_id,
            text,
            send_id=send_id,
            through_official=through_official,
            kind=kind,
        )

    def notify_typing(self, group_id: str) -> None:
        # 仅在窗口已就绪时尝试聚焦输入框，失败则忽略，避免为「正在输入」去切群。
        try:
            if self.driver.is_window_visible() and self.driver.is_logged_in() and not self.driver.is_locked():
                name = resolve_group_name(group_id, self.config.group_names)
                if self.driver.current_chat() == name:
                    self.driver.focus_input()
        except Exception:  # noqa: BLE001
            logger.debug("notify_typing 忽略异常", exc_info=True)

    def send(
        self,
        group: str,
        text: str,
        *,
        timeout: float | None = None,
        send_id: str | None = None,
        through_official: bool = False,
        kind: str = "auto_reply",
    ) -> UiaSendResult:
        """发送并返回可区分的状态与分段耗时。

        公开调用默认检查已挂接的正式闸门。HumanizedSender 在自己完成
        限速与模式判定后，以 through_official=True 进入实际 UI 操作。
        """
        wall0 = time.perf_counter()
        timings = StageTimings(preheat=self._preheat_timings)
        timeout_s = float(self.config.send_timeout_seconds if timeout is None else timeout)
        group_name = resolve_group_name(group, self.config.group_names)

        def _finish(
            ok: bool,
            status: str,
            error: str = "",
            *,
            skipped: bool = False,
            attempts: int = 0,
        ) -> UiaSendResult:
            timings.adapter_total = time.perf_counter() - wall0
            result = UiaSendResult(
                ok=ok,
                status=status,
                group=group,
                text=text,
                error=error,
                elapsed=timings.adapter_total,
                timings=timings,
                send_id=send_id,
                skipped=skipped,
                attempts=attempts,
            )
            self._last_result = result
            return result

        if not (text or "").strip():
            return _finish(False, UiaSendStatus.REJECTED, "空文本不得发送", attempts=0)

        if self._store.corrupt:
            msg = (
                "发送幂等记录已损坏，拒绝自动重发，请人工对账后恢复。"
                + (f" 原因: {self._store.corrupt_reason}" if self._store.corrupt_reason else "")
            )
            self._record_alert("idempotency_store_corrupt", msg)
            return _finish(False, UiaSendStatus.MAYBE_SENT, msg, attempts=0)

        if not through_official:
            blocked = self._blocked_by_official_gate(kind, group)
            if blocked is not None:
                status, error = blocked
                return _finish(False, status, error, attempts=0)

        if send_id:
            existing = self._store.get(send_id)
            if existing in {UiaSendStatus.OK, "sent", UiaSendStatus.MAYBE_SENT}:
                # maybe_sent 也不再重发，避免刷屏
                skipped_ok = existing in {UiaSendStatus.OK, "sent"}
                return _finish(
                    skipped_ok,
                    UiaSendStatus.OK if skipped_ok else UiaSendStatus.MAYBE_SENT,
                    error="idempotent_skip",
                    skipped=True,
                    attempts=0,
                )

        paused = self._check_pause()
        if paused is not None:
            return _finish(False, UiaSendStatus.PAUSED, paused, attempts=0)

        deadline = time.perf_counter() + max(0.05, timeout_s)
        attempts = 0
        pressed = False
        last_error = "unknown"
        last_status = UiaSendStatus.UNKNOWN
        max_tries = max(1, int(self.config.max_retries) + 1)

        while attempts < max_tries:
            attempts += 1
            if pressed:
                break
            if time.perf_counter() > deadline:
                return _finish(False, UiaSendStatus.TIMEOUT, "发送超时", attempts=attempts)

            t_safety = time.perf_counter()
            safety = self._inspect_safety()
            timings.safety += time.perf_counter() - t_safety
            if safety:
                self._trigger_pause(safety[0], safety[1])
                return _finish(False, UiaSendStatus.UI_NOT_READY, safety[1], attempts=attempts)

            try:
                self._wait_min_interval()
                visible = self._read_visible_title()
                already_open = titles_match(visible, group_name)
                timings.chat_was_open = already_open and attempts == 1
                t_open = time.perf_counter()
                if not already_open:
                    switch_wait = self._wait_group_switch()
                    timings.group_switch_wait += switch_wait
                    if time.perf_counter() > deadline:
                        raise SendTimeoutError("打开会话前已超时")
                    if not self.driver.find_chat_window():
                        raise UiNotReadyError("找不到微信聊天窗口")
                    self.driver.open_chat(group_name)
                    self._last_switch_at = self.clock.now()
                    visible = self._read_visible_title()
                    if not titles_match(visible, group_name):
                        raise ChatNotFoundError(
                            f"打开会话后窗口标题与目标群不一致（规范化精确匹配失败）: {visible!r} != {group_name!r}"
                        )
                timings.open_chat += time.perf_counter() - t_open

                t_paste = time.perf_counter()
                mid_pause = self._check_pause()
                if mid_pause is not None:
                    return _finish(False, UiaSendStatus.PAUSED, mid_pause, attempts=attempts)
                self.driver.focus_input()
                pause = self._random_between(self.config.input_pause_min, self.config.input_pause_max)
                if pause > 0:
                    self.clock.sleep(pause)
                    timings.input_pause += pause
                mid_pause = self._check_pause()
                if mid_pause is not None:
                    return _finish(False, UiaSendStatus.PAUSED, mid_pause, attempts=attempts)
                if not titles_match(self._read_visible_title(), group_name):
                    raise ChatNotFoundError("粘贴前窗口标题已不是目标群，拒绝发送以免发错群")
                if time.perf_counter() > deadline:
                    raise SendTimeoutError("粘贴前已超时")
                self.driver.paste_text(text)
                timings.paste += time.perf_counter() - t_paste

                t_send = time.perf_counter()
                mid_pause = self._check_pause()
                if mid_pause is not None:
                    return _finish(False, UiaSendStatus.PAUSED, mid_pause, attempts=attempts)
                if not titles_match(self._read_visible_title(), group_name):
                    raise ChatNotFoundError("按下发送前窗口标题已不是目标群，拒绝发送以免发错群")
                if time.perf_counter() > deadline:
                    raise SendTimeoutError("按下发送前已超时")
                # 一旦调用 press_send，无论是否抛错都视为可能已发出，不得重试。
                pressed = True
                self.driver.press_send()
                self._last_send_at = self.clock.now()
                timings.send += time.perf_counter() - t_send

                t_verify = time.perf_counter()
                verified = self.driver.verify_sent(text)
                timings.verify += time.perf_counter() - t_verify
                if verified:
                    if send_id:
                        self._store.mark(send_id, "sent")
                    with self._lock:
                        self._sent_log.append(
                            SentMessage(
                                group_id=group,
                                text=text,
                                timestamp=self.clock.now(),
                                extra={"send_id": send_id, "timings": timings.as_dict()},
                            )
                        )
                    self._maybe_return_to_primary(group_name)
                    return _finish(True, UiaSendStatus.OK, attempts=attempts)

                # 已按下发送但校验失败：不得重试
                if send_id:
                    self._store.mark(send_id, UiaSendStatus.MAYBE_SENT)
                return _finish(
                    False,
                    UiaSendStatus.MAYBE_SENT,
                    "已按下发送但未能校验成功，为避免刷屏不再重试",
                    attempts=attempts,
                )
            except ChatNotFoundError as exc:
                last_status, last_error = UiaSendStatus.TARGET_NOT_FOUND, str(exc)
                # 找不到群，重试通常无意义
                return _finish(False, last_status, last_error, attempts=attempts)
            except UiNotReadyError as exc:
                last_status, last_error = UiaSendStatus.UI_NOT_READY, str(exc)
                self._trigger_pause("ui_not_ready", str(exc))
                return _finish(False, last_status, last_error, attempts=attempts)
            except SendTimeoutError as exc:
                last_status, last_error = UiaSendStatus.TIMEOUT, str(exc)
                if pressed:
                    if send_id:
                        self._store.mark(send_id, UiaSendStatus.MAYBE_SENT)
                    return _finish(
                        False,
                        UiaSendStatus.MAYBE_SENT,
                        "按下发送后校验超时，为避免刷屏不再重试",
                        attempts=attempts,
                    )
                # 尚未按下发送，可在剩余次数内重试
                if attempts >= max_tries or time.perf_counter() > deadline:
                    return _finish(False, UiaSendStatus.TIMEOUT, last_error, attempts=attempts)
                continue
            except UiaError as exc:
                last_status = getattr(exc, "status", UiaSendStatus.UNKNOWN) or UiaSendStatus.UNKNOWN
                last_error = str(exc)
                if pressed:
                    if send_id:
                        self._store.mark(send_id, UiaSendStatus.MAYBE_SENT)
                    return _finish(False, UiaSendStatus.MAYBE_SENT, last_error, attempts=attempts)
                if attempts >= max_tries:
                    break
                continue
            except Exception as exc:  # noqa: BLE001
                # 打开会话 / 粘贴 / 发送 / 校验过程中的非 UiaError 不得冲出 send()。
                last_status = UiaSendStatus.UI_NOT_READY
                last_error = f"发送过程异常: {exc}"
                self._trigger_pause("send_unexpected", last_error)
                if pressed:
                    if send_id:
                        self._store.mark(send_id, UiaSendStatus.MAYBE_SENT)
                    return _finish(False, UiaSendStatus.MAYBE_SENT, last_error, attempts=attempts)
                return _finish(False, last_status, last_error, attempts=attempts)

        return _finish(False, last_status, last_error, attempts=attempts)

    def preheat(self, groups: list[str] | None = None) -> StageTimings:
        """预热：提前打开目标群窗口，降低首条发送的 open_chat 耗时。"""
        timings = StageTimings()
        t0 = time.perf_counter()
        paused = self._check_pause()
        if paused is not None:
            timings.adapter_total = time.perf_counter() - t0
            logger.info("预热跳过：%s", redact(paused))
            return timings
        safety = self._inspect_safety()
        if safety:
            self._trigger_pause(safety[0], safety[1])
            timings.adapter_total = time.perf_counter() - t0
            return timings
        names = [resolve_group_name(g, self.config.group_names) for g in (groups or self._preheat_targets())]
        # 只打开配置中的少量目标群，避免频繁切换
        seen: list[str] = []
        for name in names:
            if not name or name in seen:
                continue
            seen.append(name)
            if self.driver.current_chat() == name:
                continue
            t_open = time.perf_counter()
            try:
                if not self.driver.find_chat_window():
                    raise UiNotReadyError("找不到微信聊天窗口")
                self._wait_group_switch()
                self.driver.open_chat(name)
                self._last_switch_at = self.clock.now()
            except (ChatNotFoundError, UiNotReadyError, SendTimeoutError, UiaError) as exc:
                logger.warning("预热打开会话失败: %s", redact(str(exc)))
                break
            timings.open_chat += time.perf_counter() - t_open
        timings.preheat = time.perf_counter() - t0
        timings.adapter_total = timings.preheat
        timings.chat_was_open = self.driver.current_chat() in set(seen) if seen else False
        self._preheat_timings += timings.preheat
        return timings

    def keep_chat_open(self, group: str | None = None) -> bool:
        """在发送间隙把目标群窗口保持为当前会话。不发送、不放宽限频。"""
        if not self.config.keep_chat_open:
            return False
        if self.is_paused():
            return False
        safety = self._inspect_safety()
        if safety:
            self._trigger_pause(safety[0], safety[1])
            return False
        name = resolve_group_name(group or self._primary_group() or "", self.config.group_names)
        if not name:
            return False
        if self.driver.current_chat() == name:
            return True
        now = self.clock.now()
        last = self._last_reopen_at or self._last_switch_at
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < self.config.reopen_min_interval_seconds:
                return False
        try:
            self._wait_group_switch()
            self.driver.open_chat(name)
            self._last_switch_at = now
            self._last_reopen_at = now
            return True
        except UiaError as exc:
            logger.warning("保持会话打开失败: %s", redact(str(exc)))
            return False

    # --- 内部 ---

    def _preheat_targets(self) -> list[str]:
        if self.config.preheat_groups:
            return list(self.config.preheat_groups)
        primary = self._primary_group()
        return [primary] if primary else list(self.config.group_names.keys())[:1]

    def _primary_group(self) -> str | None:
        if self.config.primary_group:
            return self.config.primary_group
        if self.config.preheat_groups:
            return self.config.preheat_groups[0]
        if self.config.group_names:
            return next(iter(self.config.group_names.keys()))
        return None

    def _maybe_return_to_primary(self, just_sent_name: str) -> None:
        if not self.config.keep_chat_open or not self.config.return_to_primary_after_send:
            return
        primary = self._primary_group()
        if not primary:
            return
        primary_name = resolve_group_name(primary, self.config.group_names)
        if not primary_name or primary_name == just_sent_name:
            return
        # 受 reopen_min_interval 约束，避免刚切走又立即切回造成异常操作模式
        self.keep_chat_open(primary)

    def _read_visible_title(self) -> str | None:
        """读取真实窗口标题，不采用可能过期的 current_chat 内存值。"""
        reader = getattr(self.driver, "read_chat_title", None)
        if callable(reader):
            try:
                return reader()
            except Exception:  # noqa: BLE001
                logger.debug("读取窗口标题失败", exc_info=True)
                return None
        return None

    def _blocked_by_official_gate(self, kind: str, group_id: str) -> tuple[str, str] | None:
        """已挂接正式发送器时，直接 send 也必须经过运行模式、暂停与限速。"""
        sender = self._official_sender
        if sender is None:
            return None
        gate = getattr(sender, "send_gate", None)
        if gate is not None and hasattr(gate, "decide"):
            decision = gate.decide(kind)
            if not decision.allowed:
                status = UiaSendStatus.PAUSED if getattr(decision, "paused", False) else UiaSendStatus.REJECTED
                return status, decision.reason
        limiter = getattr(sender, "limiter", None)
        if limiter is not None and hasattr(limiter, "try_acquire"):
            kind_name = kind if kind in {"auto_reply", "announcement"} else "auto_reply"
            if not limiter.try_acquire(group_id, kind_name, self.clock.now()):
                return UiaSendStatus.REJECTED, "已达到限速配额，直接发送被拒绝"
        return None

    def _pause_flag_exists(self) -> bool:
        return self._pause_gate.is_paused()

    def _check_pause(self) -> str | None:
        if self._pause_gate.is_paused():
            return self._pause_reason or f"检测到紧急暂停标记文件: {self._pause_gate.path}"
        if self._emergency_paused:
            return self._pause_reason or "紧急暂停已开启"
        return None

    def _inspect_safety(self) -> tuple[str, str] | None:
        try:
            if self.driver.is_locked():
                return "locked", "检测到操作系统锁屏，已暂停发送"
            if not self.driver.is_window_visible():
                return "window_not_visible", "微信窗口不可见，已暂停发送"
            if not self.driver.is_logged_in():
                return "logged_out", "微信已掉线，已暂停发送（不会自动重登）"
            if self.driver.has_blocking_dialog():
                return "blocking_dialog", "检测到验证码或异常弹窗，已暂停发送（不自动处理）"
        except UiaError as exc:
            return "ui_not_ready", str(exc)
        except Exception as exc:  # noqa: BLE001
            return "ui_not_ready", f"安全探活异常: {exc}"
        return None

    def _trigger_pause(self, code: str, message: str) -> None:
        with self._lock:
            self._emergency_paused = True
            self._pause_reason = message
        # 与 D 共用 data/PAUSE 约定：异常时创建标记文件，不自动重登或打码。
        self._pause_gate.pause(reason=f"{code}: {message}")
        self._record_alert(code, message)

    def _record_alert(self, code: str, message: str) -> None:
        payload = {
            "code": code,
            "message": redact(message),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        self._alerts.append(payload)
        logger.warning("UIA 告警: %s %s", code, redact(message))
        data_dir = self.config.resolve_data_dir(self.project_root)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            with (data_dir / "uia_alerts.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            logger.debug("写入 uia_alerts.jsonl 失败", exc_info=True)
        if self._on_alert is not None:
            try:
                self._on_alert(code, payload)
            except Exception:  # noqa: BLE001
                logger.exception("告警回调失败")

    def _wait_min_interval(self) -> None:
        if self._last_send_at is None:
            return
        elapsed = (self.clock.now() - self._last_send_at).total_seconds()
        remain = self.config.min_send_interval_seconds - elapsed
        if remain > 0:
            self.clock.sleep(remain)

    def _wait_group_switch(self) -> float:
        if self._last_switch_at is None:
            return 0.0
        elapsed = (self.clock.now() - self._last_switch_at).total_seconds()
        need = self._random_between(
            self.config.group_switch_interval_min,
            self.config.group_switch_interval_max,
        )
        remain = need - elapsed
        if remain > 0:
            self.clock.sleep(remain)
            return remain
        return 0.0

    def _random_between(self, lo: float, hi: float) -> float:
        if hi <= lo:
            return max(0.0, lo)
        return self.rng.uniform(lo, hi)

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None = None,
        *,
        groups=None,
        clock: Clock | None = None,
        driver: UiDriver | None = None,
        project_root: Path | None = None,
        on_alert: AlertCallback | None = None,
    ) -> UiaOutboundAdapter:
        cfg = UiaConfig.from_mapping(config, project_root=project_root)
        if groups:
            for g in groups:
                gid = getattr(g, "group_id", None)
                name = getattr(g, "name", None)
                if gid and name and gid not in cfg.group_names:
                    cfg.group_names[str(gid)] = str(name)
        if driver is None:
            driver = _default_driver(cfg)
        return cls(
            driver=driver,
            config=cfg,
            clock=clock,
            project_root=project_root,
            on_alert=on_alert,
        )


def _default_driver(cfg: UiaConfig) -> UiDriver:
    import sys

    if sys.platform == "win32":
        from src.adapters.pyweixin_driver import PyWeixinDriver

        return PyWeixinDriver()
    if cfg.allow_fake_on_non_windows:
        logger.warning(
            "当前不是 Windows，适配器 B 使用 FakeUiDriver 模拟发送，不会触达真实微信。"
        )
        return FakeUiDriver()
    from src.adapters.pyweixin_driver import PyWeixinDriver

    # 会在首次操作时给出清晰的平台/依赖错误
    return PyWeixinDriver()


class UiaSender:
    """契约发现用发送器（方法名 send），与 UiaOutboundAdapter 共用同一实现。

    构造签名兼容契约测试：UiaSender() / UiaSender(fail_mode=...)。
    默认使用 FakeUiDriver，避免在 Windows 契约测试中误触真实微信。
    生产环境请通过 build_adapter 或传入 driver=PyWeixinDriver()。
    """

    def __init__(
        self,
        driver: UiDriver | None = None,
        fail_mode: str | None = None,
        clock: Clock | None = None,
        config: UiaConfig | None = None,
        timeout: float | None = None,
        project_root: Path | None = None,
        on_alert: AlertCallback | None = None,
        **kwargs: Any,
    ) -> None:
        import tempfile

        cfg = config or UiaConfig()
        if timeout is not None:
            cfg.send_timeout_seconds = float(timeout)
        if project_root is None and config is None:
            # 无参构造成契约测试入口：隔离 data/PAUSE，避免污染仓库或被并行测试误伤。
            project_root = Path(tempfile.mkdtemp(prefix="uia-sender-"))
            cfg.data_dir = str(project_root)
            cfg.pause_flag = str(project_root / "PAUSE")
            cfg.input_pause_min = 0.0
            cfg.input_pause_max = 0.0
            cfg.group_switch_interval_min = 0.0
            cfg.group_switch_interval_max = 0.0
            cfg.min_send_interval_seconds = 0.0
            cfg.preheat_on_start = False
        # 契约测试需要可注入失败且不能真实阻塞，因此默认假驱动。
        self._adapter = UiaOutboundAdapter(
            driver=driver or FakeUiDriver(fail_mode=fail_mode),
            config=cfg,
            clock=clock,
            project_root=project_root,
            on_alert=on_alert,
            fail_mode=fail_mode,
        )
        self.fail_mode = fail_mode
        self.scheme = "uia_sender"

    @property
    def adapter(self) -> UiaOutboundAdapter:
        return self._adapter

    @property
    def last_result(self) -> UiaSendResult | None:
        return self._adapter.last_result

    def attach_official_sender(self, sender: object) -> None:
        self._adapter.attach_official_sender(sender)

    def send(self, group: str, text: str, *, timeout: float | None = None, send_id: str | None = None, **kwargs) -> UiaSendResult:
        return self._adapter.send(group, text, timeout=timeout, send_id=send_id, **kwargs)

    def preheat(self, groups: list[str] | None = None) -> StageTimings:
        return self._adapter.preheat(groups)

    def keep_chat_open(self, group: str | None = None) -> bool:
        return self._adapter.keep_chat_open(group)

    def send_text(self, group_id: str, text: str, *, send_id: str | None = None, **kwargs) -> None:
        self._adapter.send_text(group_id, text, send_id=send_id, **kwargs)

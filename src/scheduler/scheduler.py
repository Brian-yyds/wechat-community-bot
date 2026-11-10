"""配置驱动的定时公告调度：准点 + 随机微小抖动，支持多群、启停与错过补发策略。"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from src.clock import Clock, SystemClock
from src.safety import check_forbidden, check_forbidden_many
from src.scheduler.cron import CronExpression, parse_schedule


class AnnouncementSender(Protocol):
    def submit_announcement(self, group_id: str, text: str) -> None:
        ...


@dataclass(frozen=True)
class Announcement:
    """一条定时公告配置。"""

    id: str
    text: str
    groups: tuple[str, ...]
    schedule: str
    jitter_seconds: float = 3.0
    catch_up: bool = True
    catch_up_max_minutes: float = 10.0
    enabled: bool = True
    cron: CronExpression | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FireEvent:
    """一次触发记录。"""

    announcement_id: str
    occurrence: datetime
    fire_at: datetime
    groups: tuple[str, ...]
    text: str
    reason: str  # on_time | catch_up


# 准点窗口：相对计划时刻的允许偏差（秒）。默认抖动 ≤3s，须落在 ±5s 内。
DEFAULT_PUNCTUAL_WINDOW_SECONDS = 5.0
# 兼容旧名：当作准点窗口使用。
DEFAULT_ON_TIME_GRACE_SECONDS = DEFAULT_PUNCTUAL_WINDOW_SECONDS


def announcement_from_mapping(raw: dict[str, Any], *, source: str = "config") -> Announcement:
    if not isinstance(raw, dict):
        raise ValueError(f"{source} 不是对象")
    groups = raw.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    item = Announcement(
        id=str(raw.get("id") or "").strip(),
        text=str(raw.get("text") or ""),
        groups=tuple(str(g) for g in groups if g),
        schedule=str(raw.get("schedule") or "").strip(),
        jitter_seconds=float(raw["jitter_seconds"]) if "jitter_seconds" in raw and raw["jitter_seconds"] is not None else 3.0,
        catch_up=bool(raw.get("catch_up", True)),
        catch_up_max_minutes=float(raw.get("catch_up_max_minutes") or 10),
        enabled=bool(raw.get("enabled", True)),
    )
    if not item.id:
        raise ValueError(f"{source} 缺少 id")
    if not item.text.strip():
        raise ValueError(f"公告 {item.id} 缺少 text")
    if not item.groups:
        raise ValueError(f"公告 {item.id} 缺少 groups")
    if item.jitter_seconds < 0:
        raise ValueError(f"公告 {item.id} 的 jitter_seconds 不能为负")
    check_forbidden(item.id, f"公告id:{item.id}")
    check_forbidden(item.text, f"公告text:{item.id}")
    check_forbidden_many((g, f"公告groups:{item.id}") for g in item.groups)
    cron = parse_schedule(item.schedule)
    return Announcement(
        id=item.id,
        text=item.text,
        groups=item.groups,
        schedule=item.schedule,
        jitter_seconds=item.jitter_seconds,
        catch_up=item.catch_up,
        catch_up_max_minutes=item.catch_up_max_minutes,
        enabled=item.enabled,
        cron=cron,
    )


class AnnouncementScheduler:
    """定时公告调度器。

    正常触发：在计划时刻附近发送。抖动为 [−jitter_seconds, +jitter_seconds] 的稳定值
    （由公告 id + 计划时刻哈希决定，同一轮不会因反复 tick 而变化）。
    默认 jitter_seconds=3，保证落在准点 ±5 秒窗口内。

    错过补发：若启动时或 tick 时已超过准点窗口，但距离计划时刻不超过
    catch_up_max_minutes，且 catch_up=True，则立即补发；否则跳过本轮，默认不补发超时项。
    """

    def __init__(
        self,
        announcements: Iterable[Announcement] | None = None,
        sender: AnnouncementSender | None = None,
        clock: Clock | None = None,
        state_path: str | Path | None = None,
        on_time_grace_seconds: float = DEFAULT_ON_TIME_GRACE_SECONDS,
        punctual_window_seconds: float | None = None,
        tick_interval_seconds: float = 1.0,
    ) -> None:
        self.clock = clock or SystemClock()
        self.sender = sender
        self.state_path = Path(state_path) if state_path else None
        self.punctual_window_seconds = (
            punctual_window_seconds if punctual_window_seconds is not None else on_time_grace_seconds
        )
        self.on_time_grace_seconds = self.punctual_window_seconds
        self.tick_interval_seconds = tick_interval_seconds
        self._items: dict[str, Announcement] = {}
        self._last_fired: dict[str, datetime] = {}
        self._last_skipped: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        if announcements:
            for item in announcements:
                self.add(item)
        self._load_state()

    @property
    def running(self) -> bool:
        return self._running

    def add(self, item: Announcement) -> None:
        if item.cron is None:
            item = announcement_from_mapping(
                {
                    "id": item.id,
                    "text": item.text,
                    "groups": list(item.groups),
                    "schedule": item.schedule,
                    "jitter_seconds": item.jitter_seconds,
                    "catch_up": item.catch_up,
                    "catch_up_max_minutes": item.catch_up_max_minutes,
                    "enabled": item.enabled,
                },
                source=item.id,
            )
        with self._lock:
            self._items[item.id] = item

    def remove(self, announcement_id: str) -> None:
        with self._lock:
            self._items.pop(announcement_id, None)

    def list_announcements(self) -> list[Announcement]:
        with self._lock:
            return list(self._items.values())

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="announcement-scheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = None
        with self._lock:
            self._running = False
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def tick(self, now: datetime | None = None) -> list[FireEvent]:
        """检查到期公告并提交发送。测试与演示应调用本方法，不必启动后台线程。"""
        now = now or self.clock.now()
        fired: list[FireEvent] = []
        with self._lock:
            items = list(self._items.values())
        for item in items:
            event = self._evaluate(item, now)
            if event is None:
                continue
            self._deliver(event)
            fired.append(event)
        if fired:
            self._save_state()
        return fired

    def fire_now(self, announcement_id: str) -> FireEvent:
        """立即触发指定公告（演示或人工触发），仍走发送通道。"""
        with self._lock:
            item = self._items.get(announcement_id)
        if item is None:
            raise KeyError(f"未找到公告: {announcement_id}")
        now = self.clock.now()
        event = FireEvent(
            announcement_id=item.id,
            occurrence=now.replace(second=0, microsecond=0),
            fire_at=now,
            groups=item.groups,
            text=item.text,
            reason="manual",
        )
        self._deliver(event)
        self._save_state()
        return event

    def jitter_for(self, item: Announcement, occurrence: datetime) -> float:
        """返回该计划时刻对应的稳定抖动秒数，范围 [−jitter_seconds, +jitter_seconds]。"""
        if item.jitter_seconds <= 0:
            return 0.0
        material = f"{item.id}|{occurrence.isoformat()}".encode("utf-8")
        digest = hashlib.sha256(material).digest()
        unit = int.from_bytes(digest[:8], "big") / float(2**64)
        return (unit * 2.0 - 1.0) * item.jitter_seconds

    def last_fired_at(self, announcement_id: str) -> datetime | None:
        with self._lock:
            return self._last_fired.get(announcement_id)

    def _evaluate(self, item: Announcement, now: datetime) -> FireEvent | None:
        if not item.enabled or item.cron is None:
            return None
        # 回看窗口：补发上限与准点宽限、抖动中的较大者，再留一天余量。
        lookback = int(
            max(
                item.catch_up_max_minutes * 60,
                item.jitter_seconds + self.on_time_grace_seconds,
                60,
            )
            // 60
        ) + 24 * 60
        occurrence = item.cron.previous_occurrence(now, max_lookback_minutes=lookback)
        if occurrence is None:
            return None
        with self._lock:
            last = self._last_fired.get(item.id)
            skipped = self._last_skipped.get(item.id)
        if last is not None and last >= occurrence:
            return None
        if skipped is not None and skipped >= occurrence:
            return None
        jitter = self.jitter_for(item, occurrence)
        fire_at = occurrence + timedelta(seconds=jitter)
        if now < fire_at:
            return None
        delay = (now - occurrence).total_seconds()
        if delay <= self.punctual_window_seconds:
            reason = "on_time"
        elif item.catch_up and delay <= item.catch_up_max_minutes * 60:
            reason = "catch_up"
        else:
            with self._lock:
                self._last_skipped[item.id] = occurrence
            return None
        return FireEvent(
            announcement_id=item.id,
            occurrence=occurrence,
            fire_at=fire_at,
            groups=item.groups,
            text=item.text,
            reason=reason,
        )

    def _deliver(self, event: FireEvent) -> None:
        with self._lock:
            self._last_fired[event.announcement_id] = event.occurrence
        if self.sender is not None:
            for group_id in event.groups:
                trace = self._announcement_trace(event, group_id)
                try:
                    self.sender.submit_announcement(group_id, event.text, trace=trace)
                except TypeError:
                    self.sender.submit_announcement(group_id, event.text)

    def _announcement_trace(self, event: FireEvent, group_id: str):
        """为定时公告构造延迟打点：含运行侧需要的元数据，不含正文。"""
        from src.latency import LatencyTrace

        now = self.clock.now()
        return LatencyTrace(
            message_id=f"announcement:{event.announcement_id}:{group_id}",
            group_id=group_id,
            kind="announcement",
            preview=event.text or "",
            message_at=event.fire_at or now,
            detected_at=now,
            extra={
                "announcement_id": event.announcement_id,
                "reason": event.reason,
            },
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # 后台循环不应因单次失败而退出；错误由上层日志捕获。
                pass
            self._stop.wait(self.tick_interval_seconds)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        fired = data.get("last_fired") or {}
        skipped = data.get("last_skipped") or {}
        with self._lock:
            for key, value in fired.items():
                try:
                    self._last_fired[key] = datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    continue
            for key, value in skipped.items():
                try:
                    self._last_skipped[key] = datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    continue

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        with self._lock:
            payload = {
                "last_fired": {k: v.isoformat() for k, v in self._last_fired.items()},
                "last_skipped": {k: v.isoformat() for k, v in self._last_skipped.items()},
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

"""可注入时钟，便于测试调度、限速与时间窗逻辑。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """时钟协议：返回当前时间并支持休眠。"""

    def now(self) -> datetime:
        """返回当前本地时间（无时区信息的 naive datetime，按运行环境本地时区）。"""
        ...

    def sleep(self, seconds: float) -> None:
        """休眠指定秒数。测试时钟会直接拨快时间。"""
        ...


class SystemClock:
    """系统本地时钟。"""

    def now(self) -> datetime:
        return datetime.now()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """可拨快的假时钟，供单测与演示使用。sleep 不会真实等待。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 11, 10, 0, 0)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            seconds = 0.0
        self._now += timedelta(seconds=seconds)
        self.sleeps.append(seconds)

    def set(self, when: datetime) -> None:
        self._now = when

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now

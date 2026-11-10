"""简易 cron 解析（5 字段：分 时 日 月 周）以及每日定点时间。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 6),  # 0 与 7 都表示周日
}

_DAILY_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class CronError(ValueError):
    """调度表达式不合法。"""


@dataclass(frozen=True)
class CronExpression:
    """一个 5 字段 cron 表达式。"""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    def matches(self, when: datetime) -> bool:
        weekday = when.weekday()  # 周一=0 ... 周日=6
        # cron 周日=0，Python weekday 周日=6。转换为 cron： (weekday + 1) % 7
        cron_weekday = (weekday + 1) % 7
        return (
            when.minute in self.minutes
            and when.hour in self.hours
            and when.day in self.days
            and when.month in self.months
            and cron_weekday in self.weekdays
        )

    def previous_occurrence(self, when: datetime, max_lookback_minutes: int = 60 * 24 * 8) -> datetime | None:
        """返回 when 之前（含对齐到整分且 <= when）最近一次匹配时刻。

        为避免长时间回看，lookback 以分钟为步长，默认最多 8 天。
        """
        cursor = when.replace(second=0, microsecond=0)
        if cursor > when:
            cursor -= timedelta(minutes=1)
        # 若 when 本身不是整分，cursor 已经是当前整分；若当前整分 > when 不会发生。
        # 我们允许等于 when 的整分（当 when 正好落在某分钟）。
        if when.second or when.microsecond:
            # 未到该分钟结束，不算已到达该分钟的调度点（调度点视为该分钟的 00 秒）。
            if cursor == when.replace(second=0, microsecond=0) and not self.matches(cursor):
                pass
            elif self.matches(cursor) and cursor <= when:
                return cursor
            else:
                cursor -= timedelta(minutes=1)
        for _ in range(max_lookback_minutes + 1):
            if self.matches(cursor):
                return cursor
            cursor -= timedelta(minutes=1)
        return None

    def next_occurrence(self, when: datetime, max_lookahead_minutes: int = 60 * 24 * 8) -> datetime | None:
        cursor = when.replace(second=0, microsecond=0)
        if cursor <= when:
            cursor += timedelta(minutes=1)
        for _ in range(max_lookahead_minutes + 1):
            if self.matches(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return None


def parse_schedule(expr: str) -> CronExpression:
    """解析 'HH:MM' 每日定点，或标准 5 字段 cron。"""
    text = (expr or "").strip()
    if not text:
        raise CronError("调度表达式为空")
    daily = _DAILY_RE.match(text)
    if daily:
        hour = int(daily.group(1))
        minute = int(daily.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise CronError(f"每日定点时间不合法: {expr}")
        return CronExpression(
            source=text,
            minutes=frozenset([minute]),
            hours=frozenset([hour]),
            days=frozenset(range(1, 32)),
            months=frozenset(range(1, 13)),
            weekdays=frozenset(range(0, 7)),
        )
    parts = text.split()
    if len(parts) != 5:
        raise CronError(f"无法解析调度表达式（需要 HH:MM 或 5 字段 cron）: {expr}")
    names = ("minute", "hour", "day", "month", "weekday")
    values = [_parse_field(part, name) for part, name in zip(parts, names)]
    return CronExpression(
        source=text,
        minutes=values[0],
        hours=values[1],
        days=values[2],
        months=values[3],
        weekdays=values[4],
    )


def _parse_field(field: str, name: str) -> frozenset[int]:
    lo, hi = _FIELD_RANGES[name]
    out: set[int] = set()
    for item in field.split(","):
        item = item.strip()
        if not item:
            raise CronError(f"{name} 字段为空: {field}")
        step = 1
        body = item
        if "/" in item:
            body, step_s = item.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronError(f"{name} 步长不合法: {item}") from exc
            if step <= 0:
                raise CronError(f"{name} 步长必须为正: {item}")
        if body in {"*", "?"}:
            start, end = lo, hi
        elif "-" in body:
            a, b = body.split("-", 1)
            start, end = _int_in_range(a, name, lo, hi), _int_in_range(b, name, lo, hi)
            if start > end:
                raise CronError(f"{name} 范围起止颠倒: {item}")
        else:
            start = end = _int_in_range(body, name, lo, hi)
        for value in range(start, end + 1, step):
            if name == "weekday" and value == 7:
                out.add(0)
            else:
                out.add(value)
    return frozenset(out)


def _int_in_range(raw: str, name: str, lo: int, hi: int) -> int:
    # weekday 允许 7 表示周日
    upper = 7 if name == "weekday" else hi
    try:
        value = int(raw)
    except ValueError as exc:
        raise CronError(f"{name} 不是整数: {raw}") from exc
    if value < lo or value > upper:
        raise CronError(f"{name} 超出范围 {lo}-{upper}: {value}")
    return value

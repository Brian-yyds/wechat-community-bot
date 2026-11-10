"""延迟分段打点：检出 → 决策 → 入队 → 发出。

为每条消息记录：

- detected_at：适配器检出时间
- classified_at / decided_at：管线分类与最终决策时间
- queued_at：进入拟人发送队列
- sent_at：适配器真正发出（或丢弃时的判定时间）

并计算分段耗时：

- 检出延迟：message_at → detected_at
- 决策耗时：以单调时钟测量的管线决策时间（不受 FakeClock 拨快影响）
- 限速/拟人延迟：queued_at → 开始调用适配器发送（含限速等待、拟人停顿、打字）
- 拟人延迟：故意加入的随机停顿，单独列出，避免拿它去误判 3s/8s 目标
- 适配器发送耗时：调用 send_text 的耗时

记录以 JSONL 写入 ``data/latency.jsonl``。提供 p50 / p95 / 最大 的汇总。

目标（评估时把拟人延迟单独列出）：

- 决策耗时应远小于 100ms
- 已打开会话：端到端（不含拟人）≤ 3s
- 其余：端到端（不含拟人）≤ 8s
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from src.logging_util import redact

# 对照目标。拟人延迟是刻意的，不计入这两项端到端预算。
DECISION_TARGET_S = 0.100
OPEN_SESSION_TARGET_S = 3.0
OTHER_TARGET_S = 8.0

LATENCY_JSONL_NAME = "latency.jsonl"

_SEGMENT_KEYS = (
    "detect_delay_s",
    "decision_s",
    "rate_wait_s",
    "humanize_delay_s",
    "typing_delay_s",
    "rate_humanize_delay_s",
    "adapter_send_s",
    "e2e_s",
    "e2e_excluding_humanize_s",
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds")


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数。``p`` 可以是 50 / 95，也可以是 0.50 / 0.95。"""
    if not values:
        return None
    if p > 1:
        p = p / 100.0
    p = min(1.0, max(0.0, float(p)))
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _segment_stats(values: Sequence[float]) -> dict[str, float | int | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "p50": percentile(clean, 50),
        "p95": percentile(clean, 95),
        "max": max(clean),
    }


@dataclass
class LatencyTrace:
    """单条消息（及其可能的自动回复）的延迟打点。"""

    message_id: str = ""
    group_id: str = ""
    kind: str = "auto_reply"
    session_open: bool | None = None
    outcome: str = "pending"
    drop_reason: str = ""
    preview: str = ""

    message_at: datetime | None = None
    detected_at: datetime | None = None
    classified_at: datetime | None = None
    decided_at: datetime | None = None
    queued_at: datetime | None = None
    send_started_at: datetime | None = None
    sent_at: datetime | None = None

    # 决策与适配器发送用单调时钟，避免被测试时钟拨快干扰。
    decision_s: float | None = None
    adapter_send_s: float | None = None
    rate_wait_s: float = 0.0
    humanize_delay_s: float = 0.0
    typing_delay_s: float = 0.0

    extra: dict[str, Any] = field(default_factory=dict)

    def compute_segments(self) -> dict[str, float | None]:
        detect = _seconds(self.message_at, self.detected_at)
        rate_humanize = self.rate_wait_s + self.humanize_delay_s + self.typing_delay_s
        if self.queued_at and self.send_started_at:
            queued_span = _seconds(self.queued_at, self.send_started_at)
            if queued_span is not None:
                rate_humanize = max(rate_humanize, queued_span)
        e2e_end = self.sent_at or self.decided_at
        e2e_start = self.detected_at or self.message_at
        e2e = _seconds(e2e_start, e2e_end)
        e2e_ex = None
        if e2e is not None:
            e2e_ex = max(0.0, e2e - self.humanize_delay_s)
        return {
            "detect_delay_s": detect,
            "decision_s": self.decision_s,
            "rate_wait_s": self.rate_wait_s,
            "humanize_delay_s": self.humanize_delay_s,
            "typing_delay_s": self.typing_delay_s,
            "rate_humanize_delay_s": rate_humanize,
            "adapter_send_s": self.adapter_send_s,
            "e2e_s": e2e,
            "e2e_excluding_humanize_s": e2e_ex,
        }

    def to_record(self) -> dict[str, Any]:
        """转成可落盘的字典；预览等文本会脱敏。"""
        segments = self.compute_segments()
        record = {
            "message_id": redact(self.message_id) if self.message_id else "",
            "group_id": redact(self.group_id) if self.group_id else "",
            "kind": self.kind,
            "session_open": self.session_open,
            "outcome": self.outcome,
            "drop_reason": redact(self.drop_reason) if self.drop_reason else "",
            "preview": redact(self.preview) if self.preview else "",
            "message_at": _iso(self.message_at),
            "detected_at": _iso(self.detected_at),
            "classified_at": _iso(self.classified_at),
            "decided_at": _iso(self.decided_at),
            "queued_at": _iso(self.queued_at),
            "send_started_at": _iso(self.send_started_at),
            "sent_at": _iso(self.sent_at),
            "segments": segments,
        }
        if self.extra:
            safe_extra = {}
            for key, value in self.extra.items():
                safe_extra[str(key)] = redact(value) if isinstance(value, str) else value
            record["extra"] = safe_extra
        return record


class LatencyStore:
    """把打点追加写入 JSONL，并提供汇总。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pending: dict[str, LatencyTrace] = {}

    def _key(self, trace: LatencyTrace) -> str:
        return trace.message_id or f"anon-{id(trace)}"

    def remember(self, trace: LatencyTrace) -> None:
        with self._lock:
            self._pending[self._key(trace)] = trace

    def pop_pending(self, message_id: str) -> LatencyTrace | None:
        with self._lock:
            return self._pending.pop(message_id, None)

    def write(self, trace: LatencyTrace) -> dict[str, Any]:
        record = trace.to_record()
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._pending.pop(self._key(trace), None)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return record

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def summarize(self, records: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return summarize_traces(records if records is not None else self.read_all())


def _iter_segment_maps(records: Iterable[Mapping[str, Any] | LatencyTrace]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in records:
        if isinstance(item, LatencyTrace):
            record = item.to_record()
        else:
            record = dict(item)
        segments = dict(record.get("segments") or {})
        segments["session_open"] = record.get("session_open")
        segments["outcome"] = record.get("outcome")
        out.append(segments)
    return out


def summarize_traces(records: Iterable[Mapping[str, Any] | LatencyTrace]) -> dict[str, Any]:
    """按分段汇总 p50 / p95 / 最大；并按是否已打开会话拆开。"""
    rows = _iter_segment_maps(records)

    def _collect(subset: list[dict[str, Any]]) -> dict[str, Any]:
        by_segment = {key: _segment_stats([row[key] for row in subset if row.get(key) is not None]) for key in _SEGMENT_KEYS}
        return {"count": len(subset), "by_segment": by_segment}

    open_rows = [row for row in rows if row.get("session_open") is True]
    other_rows = [row for row in rows if row.get("session_open") is not True]
    summary = {
        "count": len(rows),
        "all": _collect(rows),
        "open_session": _collect(open_rows),
        "other": _collect(other_rows),
        "targets": {
            "decision_s": DECISION_TARGET_S,
            "open_session_e2e_s": OPEN_SESSION_TARGET_S,
            "other_e2e_s": OTHER_TARGET_S,
            "note": (
                "拟人延迟是刻意加入的随机停顿，评估已打开会话 ≤3s / 其余 ≤8s 时"
                "应看 e2e_excluding_humanize_s，并把 humanize_delay_s 单独列出，以免误判。"
            ),
        },
    }
    return summary


def format_summary(summary: dict[str, Any]) -> str:
    """把汇总格式化成适合自检输出的中文短文。"""
    all_seg = (summary.get("all") or {}).get("by_segment") or {}

    def _fmt(key: str, digits: int = 3) -> str:
        stats = all_seg.get(key) or {}
        if not stats or not stats.get("count"):
            return "无样本"
        p50 = stats.get("p50")
        p95 = stats.get("p95")
        mx = stats.get("max")
        return f"p50={p50:.{digits}f}s p95={p95:.{digits}f}s 最大={mx:.{digits}f}s"

    decision = all_seg.get("decision_s") or {}
    decision_max = decision.get("max")
    if decision_max is None:
        decision_text = "无样本"
        decision_ok = True
    else:
        # 决策通常远小于 1 秒，用毫秒更直观。
        decision_text = f"最大 {decision_max * 1000:.1f}ms（p50={(decision.get('p50') or 0) * 1000:.1f}ms）"
        decision_ok = decision_max < DECISION_TARGET_S
    lines = [
        f"样本 {summary.get('count', 0)} 条",
        f"决策耗时 {decision_text}，目标远小于 {int(DECISION_TARGET_S * 1000)}ms"
        + ("，达标" if decision_ok else "，未达标"),
        f"检出延迟 {_fmt('detect_delay_s')}",
        f"拟人延迟（刻意，单独列出） {_fmt('humanize_delay_s')}",
        f"限速/拟人合计 {_fmt('rate_humanize_delay_s')}",
        f"适配器发送 {_fmt('adapter_send_s')}",
        f"端到端不含拟人 {_fmt('e2e_excluding_humanize_s')}（已打开会话目标 ≤{OPEN_SESSION_TARGET_S:.0f}s，其余 ≤{OTHER_TARGET_S:.0f}s）",
        f"端到端含拟人 {_fmt('e2e_s')}",
    ]
    return "；".join(lines)


def measure_decision(func, *args, **kwargs):
    """用单调时钟测量一次调用的耗时（秒）并返回 (结果, 秒)。"""
    started = time.perf_counter()
    result = func(*args, **kwargs)
    return result, time.perf_counter() - started

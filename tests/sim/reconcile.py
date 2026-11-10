# -*- coding: utf-8 -*-
"""对账：库内应有消息 vs 机器人处理过的消息。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ReconcileReport:
    expected: int
    processed_unique: int
    missed_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    extra_ids: list[str] = field(default_factory=list)
    miss_rate: float = 0.0
    dup_process_rate: float = 0.0

    @property
    def ok(self) -> bool:
        return self.miss_rate == 0.0 and self.dup_process_rate == 0.0 and not self.extra_ids


def reconcile(
    expected_ids: Iterable[str],
    processed_ids: Iterable[str],
) -> ReconcileReport:
    """processed_ids 可含重复（表示同一消息被处理多次）。

    漏抓率 = 库内有但一次都没处理 / 库内唯一消息数。
    重复处理率 = 被处理超过一次的唯一消息数 / 库内唯一消息数。
    """
    expected = [str(x) for x in expected_ids]
    expected_set = set(expected)
    processed = [str(x) for x in processed_ids]
    counts: dict[str, int] = {}
    for mid in processed:
        counts[mid] = counts.get(mid, 0) + 1
    processed_set = set(counts)
    missed = sorted(expected_set - processed_set)
    dups = sorted(mid for mid, n in counts.items() if n > 1 and mid in expected_set)
    extra = sorted(processed_set - expected_set)
    denom = len(expected_set) or 1
    return ReconcileReport(
        expected=len(expected_set),
        processed_unique=len(processed_set & expected_set),
        missed_ids=missed,
        duplicate_ids=dups,
        extra_ids=extra,
        miss_rate=len(missed) / denom if expected_set else 0.0,
        dup_process_rate=len(dups) / denom if expected_set else 0.0,
    )

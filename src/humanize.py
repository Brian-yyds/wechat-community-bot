"""按场景分档的拟人延迟。

默认档位（均可被配置覆盖）：

- open_session_rule：已打开会话的规则回复，1.5–2.4 秒。
  默认下限不得低于 1.5 秒（可配置下调）；上限 2.4 秒 + 检出/决策预算 0.5 秒 ≤ 3 秒端到端目标。
- other / 未打开会话：2–6 秒，对应其余场景 8 秒目标。
- announcement：公告档，默认与 other 相同，可按公告配置单独覆盖。

取舍说明：默认 2–6 秒与「已打开会话 3 秒」冲突（仅下限就占 2 秒），
因此打开会话必须走短区间，不得把 2–6 秒硬套到 3 秒目标上。

``humanize_delay`` 可注入随机源，不真实睡眠；空文本落在区间内，
长文本会附加打字时间，但仍不超过该路径的端到端预算。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Mapping

# 路径名（核查用例使用 open_session_rule）。
PATH_OPEN_SESSION_RULE = "open_session_rule"
PATH_OTHER = "other"
PATH_ANNOUNCEMENT = "announcement"

# 别名，便于配置或调用时使用近义名称。
_PATH_ALIASES = {
    "": PATH_OTHER,
    "default": PATH_OTHER,
    "other": PATH_OTHER,
    "unopened": PATH_OTHER,
    "unopened_session": PATH_OTHER,
    "rest": PATH_OTHER,
    "open_session_rule": PATH_OPEN_SESSION_RULE,
    "open_session": PATH_OPEN_SESSION_RULE,
    "open": PATH_OPEN_SESSION_RULE,
    "announcement": PATH_ANNOUNCEMENT,
    "announce": PATH_ANNOUNCEMENT,
}

# 默认区间。
DEFAULT_OTHER = (2.0, 6.0)
DEFAULT_OPEN_SESSION = (1.5, 2.4)
DEFAULT_ANNOUNCEMENT = (2.0, 6.0)

OPEN_SESSION_MIN_FLOOR = 1.5
GLOBAL_MIN_FLOOR = 1.5
GLOBAL_MAX_CEILING = 8.0

# 打开会话路径：拟人上限 + 该预算 ≤ 3 秒。
OPEN_SESSION_TARGET_S = 3.0
OTHER_TARGET_S = 8.0
DETECT_DECIDE_BUDGET_S = 0.5

# 打字速度（秒/字），带轻微抖动。
_TYPING_PER_CHAR_MIN = 0.03
_TYPING_PER_CHAR_MAX = 0.05


class HumanizeConfigError(ValueError):
    """拟人延迟配置非法，或与对应路径的 P95 目标不相容。"""


def normalize_path(path: str | None) -> str:
    """把调用方传入的 path 归一化为标准档名。缺省为 other。"""
    if path is None:
        return PATH_OTHER
    key = str(path).strip().lower()
    if key not in _PATH_ALIASES:
        raise HumanizeConfigError(
            f"未知拟人延迟档 {path!r}，应为 open_session_rule / other / announcement"
        )
    return _PATH_ALIASES[key]


@dataclass(frozen=True)
class HumanizeProfiles:
    """三档拟人区间。"""

    other: tuple[float, float] = DEFAULT_OTHER
    open_session_rule: tuple[float, float] = DEFAULT_OPEN_SESSION
    announcement: tuple[float, float] = DEFAULT_ANNOUNCEMENT
    # 默认硬约束 1.5s；若需采用核查建议的 1.0s 下限，可通过配置下调（不建议低于 0.8s）。
    min_floor: float = OPEN_SESSION_MIN_FLOOR

    def range_for(self, path: str | None = None) -> tuple[float, float]:
        name = normalize_path(path)
        if name == PATH_OPEN_SESSION_RULE:
            return self.open_session_rule
        if name == PATH_ANNOUNCEMENT:
            return self.announcement
        return self.other

    def target_for(self, path: str | None = None) -> float:
        name = normalize_path(path)
        if name == PATH_OPEN_SESSION_RULE:
            return OPEN_SESSION_TARGET_S
        return OTHER_TARGET_S


def _pair(lo: float, hi: float) -> tuple[float, float]:
    return (float(lo), float(hi))


def _from_mapping(raw: Mapping[str, Any] | None, default: tuple[float, float]) -> tuple[float, float]:
    if not raw:
        return default
    lo = raw.get("min", raw.get("send_delay_min", default[0]))
    hi = raw.get("max", raw.get("send_delay_max", default[1]))
    return _pair(lo, hi)


def profiles_from_mapping(section: Mapping[str, Any] | None) -> HumanizeProfiles:
    """从 rate_limit 配置段读取分档区间，缺省用默认值。"""
    section = section or {}
    humanize = section.get("humanize") or {}
    other = _pair(section.get("send_delay_min", DEFAULT_OTHER[0]), section.get("send_delay_max", DEFAULT_OTHER[1]))
    if "other" in humanize:
        other = _from_mapping(humanize.get("other"), other)
    open_lo = section.get("open_session_delay_min", humanize.get("open_session_delay_min", DEFAULT_OPEN_SESSION[0]))
    open_hi = section.get("open_session_delay_max", humanize.get("open_session_delay_max", DEFAULT_OPEN_SESSION[1]))
    open_range = _from_mapping(humanize.get("open_session_rule") or humanize.get("open_session"), (open_lo, open_hi))
    ann_default = (
        section.get("announcement_delay_min", other[0]),
        section.get("announcement_delay_max", other[1]),
    )
    if ann_default[0] is None:
        ann_default = (other[0], ann_default[1])
    if ann_default[1] is None:
        ann_default = (ann_default[0], other[1])
    announcement = _from_mapping(humanize.get("announcement"), _pair(*ann_default))
    floor = section.get("humanize_min_floor", humanize.get("min_floor", OPEN_SESSION_MIN_FLOOR))
    return HumanizeProfiles(
        other=other,
        open_session_rule=open_range,
        announcement=announcement,
        min_floor=float(floor),
    )


def humanize_delay_range(
    path: str | None = None,
    *,
    profiles: HumanizeProfiles | None = None,
) -> tuple[float, float]:
    """返回指定档的拟人区间（秒）。缺省 path 为其余/未打开会话档。"""
    return (profiles or HumanizeProfiles()).range_for(path)


def typing_delay(
    text: str,
    rng: random.Random,
    *,
    cap: float,
) -> float:
    """按文本长度计算附加打字时间。空文本为 0，结果不超过 cap。"""
    if cap <= 0:
        return 0.0
    n = len(text or "")
    if n <= 0:
        return 0.0
    per = rng.uniform(_TYPING_PER_CHAR_MIN, _TYPING_PER_CHAR_MAX)
    return min(float(n) * per, cap)


def humanize_delay(
    text: str = "",
    *,
    rng: random.Random | None = None,
    path: str | None = None,
    profiles: HumanizeProfiles | None = None,
) -> float:
    """计算一次拟人延迟（秒），不睡眠。

    空文本落在该档区间内；非空文本附加打字时间，且不超过该路径端到端预算。
    """
    source = rng or random.Random()
    pack = profiles or HumanizeProfiles()
    name = normalize_path(path)
    lo, hi = pack.range_for(name)
    if hi <= lo:
        return max(0.0, lo)
    budget = pack.target_for(name)
    if name == PATH_OPEN_SESSION_RULE:
        # 打开会话：打字时间必须含在短区间内，总计不得超过上限（保证端到端 ≤3s）。
        extra = typing_delay(text, source, cap=max(0.0, hi - lo))
        upper = max(lo, hi - extra)
        base = source.uniform(lo, upper) if upper > lo else lo
        return min(hi, base + extra)
    base = source.uniform(lo, hi)
    # 其余档：打字时间加在区间之上，但不超过该路径端到端预算（默认 8s）。
    extra = typing_delay(text, source, cap=max(0.0, budget - base))
    return base + extra


def validate_latency_config(
    p95_max: float | None = None,
    process_budget: float | None = None,
    path: str | None = None,
    *,
    profiles: HumanizeProfiles | None = None,
) -> bool:
    """校验区间合法，且与该路径的 P95 目标相容。非法则抛出 HumanizeConfigError。"""
    pack = profiles or HumanizeProfiles()
    name = normalize_path(path)
    lo, hi = pack.range_for(name)
    if name == PATH_OPEN_SESSION_RULE:
        floor = pack.min_floor
        target = OPEN_SESSION_TARGET_S
        budget = DETECT_DECIDE_BUDGET_S if process_budget is None else process_budget
    else:
        floor = GLOBAL_MIN_FLOOR
        target = OTHER_TARGET_S
        # 保持对旧调用 validate_latency_config(p95_max=12, process_budget=2) 的兼容。
        budget = 2.0 if process_budget is None else process_budget
    ceiling = GLOBAL_MAX_CEILING
    if p95_max is None:
        p95_max = target if name == PATH_OPEN_SESSION_RULE else 12.0
    if not (floor <= lo < hi <= ceiling):
        raise HumanizeConfigError(
            f"拟人化区间非法: {(lo, hi)}（档 {name} 须满足 {floor}s ≤ 下限 < 上限 ≤ {ceiling}s）"
        )
    if hi + budget > p95_max + 1e-9:
        raise HumanizeConfigError(
            f"拟人化上限 {hi}s + 检出/决策预算 {budget}s 超过该路径 P95 目标 {p95_max}s（档 {name}）"
        )
    if name == PATH_OPEN_SESSION_RULE:
        other_hi = pack.other[1]
        if hi >= other_hi:
            raise HumanizeConfigError(
                "打开会话短区间应窄于默认 2–6s，否则无法同时照顾拟人停顿与 3s 目标"
            )
    return True


def validate_profiles(profiles: HumanizeProfiles) -> bool:
    """校验全部档位。"""
    validate_latency_config(path=PATH_OTHER, profiles=profiles, p95_max=OTHER_TARGET_S, process_budget=DETECT_DECIDE_BUDGET_S)
    validate_latency_config(path=PATH_OPEN_SESSION_RULE, profiles=profiles)
    validate_latency_config(path=PATH_ANNOUNCEMENT, profiles=profiles, p95_max=OTHER_TARGET_S, process_budget=DETECT_DECIDE_BUDGET_S)
    return True

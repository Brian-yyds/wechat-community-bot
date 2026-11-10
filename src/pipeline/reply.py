"""可配置规则回复：关键词、正则、问答库；支持冷却、群白名单与优先级。"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.adapters.base import Message
from src.clock import Clock, SystemClock
from src.safety import ForbiddenContentError, check_forbidden, check_forbidden_many


@dataclass(frozen=True)
class ReplyRule:
    """一条自动回复规则。priority 越大越优先。"""

    id: str
    reply: str
    priority: int = 0
    keywords: tuple[str, ...] = ()
    pattern: str | None = None
    cooldown_seconds: float = 0.0
    groups: tuple[str, ...] = ("*",)
    enabled: bool = True
    match_all_keywords: bool = False


@dataclass(frozen=True)
class ReplyMatch:
    """一次规则命中。"""

    rule: ReplyRule
    reply_text: str


class ReplyEngine:
    """按优先级匹配回复规则，并执行冷却与群白名单过滤。"""

    def __init__(
        self,
        rules: Iterable[ReplyRule] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._rules: list[ReplyRule] = []
        self._compiled: dict[str, re.Pattern[str]] = {}
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._lock = threading.Lock()
        if rules:
            for rule in rules:
                self.add_rule(rule)

    @property
    def rules(self) -> list[ReplyRule]:
        return list(self._rules)

    def add_rule(self, rule: ReplyRule) -> None:
        self._validate(rule)
        compiled = None
        if rule.pattern:
            try:
                compiled = re.compile(rule.pattern)
            except re.error as exc:
                raise ValueError(f"回复规则 {rule.id} 的正则无效: {exc}") from exc
        with self._lock:
            self._rules = [r for r in self._rules if r.id != rule.id]
            self._rules.append(rule)
            self._rules.sort(key=lambda r: r.priority, reverse=True)
            if compiled is not None:
                self._compiled[rule.id] = compiled
            else:
                self._compiled.pop(rule.id, None)

    def load_from_mappings(self, items: Iterable[dict[str, Any]], *, source: str = "config") -> None:
        for index, raw in enumerate(items):
            rule = rule_from_mapping(raw, source=f"{source}[{index}]")
            self.add_rule(rule)

    def load_from_file(self, path: str | Path) -> None:
        """从 YAML / JSON / TOML 加载问答库或规则列表。"""
        from src.config_util import load_file

        data = load_file(Path(path))
        items = _extract_rule_items(data)
        self.load_from_mappings(items, source=str(path))

    def match(self, message: Message) -> ReplyMatch | None:
        """返回命中的最高优先级规则；冷却期内的规则会被跳过。"""
        now = self._clock.now()
        with self._lock:
            rules = list(self._rules)
            compiled = dict(self._compiled)
        for rule in rules:
            if not rule.enabled:
                continue
            if not self._group_allowed(rule, message.group_id):
                continue
            if not self._content_hit(rule, message.content or "", compiled.get(rule.id)):
                continue
            if self._in_cooldown(rule, message.group_id, now):
                continue
            self._mark_cooldown(rule, message.group_id, now)
            return ReplyMatch(rule=rule, reply_text=rule.reply)
        return None

    def _in_cooldown(self, rule: ReplyRule, group_id: str, now: datetime) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        with self._lock:
            last = self._cooldowns.get((rule.id, group_id))
        if last is None:
            return False
        return now < last + timedelta(seconds=rule.cooldown_seconds)

    def _mark_cooldown(self, rule: ReplyRule, group_id: str, now: datetime) -> None:
        if rule.cooldown_seconds <= 0:
            return
        with self._lock:
            self._cooldowns[(rule.id, group_id)] = now

    @staticmethod
    def _group_allowed(rule: ReplyRule, group_id: str) -> bool:
        if not rule.groups or "*" in rule.groups:
            return True
        return group_id in rule.groups

    @staticmethod
    def _content_hit(rule: ReplyRule, content: str, compiled: re.Pattern[str] | None) -> bool:
        keyword_hit = False
        if rule.keywords:
            if rule.match_all_keywords:
                keyword_hit = all(k in content for k in rule.keywords if k)
            else:
                keyword_hit = any(k in content for k in rule.keywords if k)
        pattern_hit = bool(compiled and compiled.search(content))
        if rule.keywords and rule.pattern:
            return keyword_hit or pattern_hit
        if rule.keywords:
            return keyword_hit
        if rule.pattern:
            return pattern_hit
        return False

    @staticmethod
    def _validate(rule: ReplyRule) -> None:
        if not rule.id or not str(rule.id).strip():
            raise ValueError("回复规则缺少 id")
        if not rule.reply or not str(rule.reply).strip():
            raise ValueError(f"回复规则 {rule.id} 缺少 reply")
        if not rule.keywords and not rule.pattern:
            raise ValueError(f"回复规则 {rule.id} 需要 keywords 或 pattern")
        check_forbidden(rule.id, f"回复规则id:{rule.id}")
        check_forbidden(rule.reply, f"回复规则reply:{rule.id}")
        check_forbidden(rule.pattern, f"回复规则pattern:{rule.id}")
        check_forbidden_many((kw, f"回复规则keyword:{rule.id}") for kw in rule.keywords)


def rule_from_mapping(raw: dict[str, Any], *, source: str = "config") -> ReplyRule:
    if not isinstance(raw, dict):
        raise ValueError(f"{source} 不是对象")
    keywords = raw.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    groups = raw.get("groups") or ["*"]
    if isinstance(groups, str):
        groups = [groups]
    rule = ReplyRule(
        id=str(raw.get("id") or "").strip(),
        reply=str(raw.get("reply") or ""),
        priority=int(raw.get("priority") or 0),
        keywords=tuple(str(k) for k in keywords if k),
        pattern=raw.get("pattern"),
        cooldown_seconds=float(raw.get("cooldown_seconds") or 0),
        groups=tuple(str(g) for g in groups),
        enabled=bool(raw.get("enabled", True)),
        match_all_keywords=bool(raw.get("match_all_keywords", False)),
    )
    return rule


def _extract_rule_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("问答库文件应为列表或包含 replies/rules 的对象")
    for key in ("replies", "rules", "qa", "items"):
        if key in data and isinstance(data[key], list):
            return data[key]
    raise ValueError("问答库文件中未找到 replies/rules 列表")

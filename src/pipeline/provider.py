"""自动回复提供者：规则/问答库为默认实现，LLM 等只留抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.adapters.base import Message
from src.pipeline.classify import Category
from src.pipeline.extract import Extraction
from src.pipeline.reply import ReplyEngine, ReplyMatch


@dataclass
class ReplyContext:
    """回复决策时可参考的上下文。"""

    category: Category | None = None
    extraction: Extraction | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyProposal:
    """一次回复建议。"""

    text: str
    source: str  # rule / qa / llm / ...
    rule_id: str | None = None


class ReplyProvider(ABC):
    """自动回复提供者抽象。后续接入 LLM 时实现本接口即可，不必改管线。"""

    @abstractmethod
    def propose(self, message: Message, context: ReplyContext | None = None) -> ReplyProposal | None:
        """若应回复则返回文案，否则返回 None。不得给出买卖建议或被禁业务指引。"""


class RuleReplyProvider(ReplyProvider):
    """基于关键词 / 正则 / 问答库的默认实现。"""

    def __init__(self, engine: ReplyEngine | None = None) -> None:
        self.engine = engine or ReplyEngine()

    def propose(self, message: Message, context: ReplyContext | None = None) -> ReplyProposal | None:
        match: ReplyMatch | None = self.engine.match(message)
        if match is None:
            return None
        return ReplyProposal(text=match.reply_text, source="rule", rule_id=match.rule.id)


class ChainReplyProvider(ReplyProvider):
    """按顺序询问多个提供者，返回第一个非空建议。默认把规则放在前面。"""

    def __init__(self, providers: list[ReplyProvider]) -> None:
        self.providers = list(providers)

    def propose(self, message: Message, context: ReplyContext | None = None) -> ReplyProposal | None:
        for provider in self.providers:
            proposal = provider.propose(message, context)
            if proposal is not None and proposal.text.strip():
                return proposal
        return None

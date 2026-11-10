"""消息处理管线：去重、分类、要点提取、规则回复、结构化落盘。"""

from src.pipeline.classify import Category, Classifier
from src.pipeline.dedup import Deduplicator
from src.pipeline.extract import Extraction, Extractor
from src.pipeline.pipeline import Pipeline, ProcessResult
from src.pipeline.guard import IdentityConfig, ReplyGuard, StormConfig
from src.pipeline.provider import ReplyProvider, RuleReplyProvider
from src.pipeline.reply import ReplyEngine, ReplyRule
from src.pipeline.sink import JsonlSink

__all__ = [
    "Category",
    "Classifier",
    "Deduplicator",
    "Extraction",
    "Extractor",
    "JsonlSink",
    "Pipeline",
    "ProcessResult",
    "ReplyEngine",
    "ReplyGuard",
    "ReplyProvider",
    "ReplyRule",
    "RuleReplyProvider",
    "StormConfig",
    "IdentityConfig",
]

"""消息处理管线：串联去重、提取、分类、规则回复并落盘。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from src.adapters.base import Event, EventType, Message
from src.clock import Clock, SystemClock
from src.latency import LatencyStore, LatencyTrace
from src.pipeline.classify import Category, Classifier
from src.pipeline.dedup import Deduplicator
from src.pipeline.extract import Extraction, Extractor
from src.pipeline.guard import GuardDecision, ReplyGuard
from src.pipeline.provider import ReplyContext, ReplyProposal, ReplyProvider, RuleReplyProvider
from src.pipeline.reply import ReplyEngine
from src.pipeline.sink import JsonlSink


class ReplySender(Protocol):
    """管线向外发送自动回复的接口（由拟人发送器实现）。"""

    def submit_auto_reply(self, group_id: str, text: str, *, trace: LatencyTrace | None = None) -> None:
        ...


@dataclass
class ProcessResult:
    """单条消息的处理结果。"""

    message: Message
    duplicate: bool
    category: Category | None
    extraction: Extraction | None
    reply_id: str | None
    reply_text: str | None
    record: dict
    skipped: bool = False
    skip_reason: str | None = None
    trace: LatencyTrace | None = None


class Pipeline:
    """完整处理一条群消息事件。"""

    def __init__(
        self,
        dedup: Deduplicator | None = None,
        classifier: Classifier | None = None,
        extractor: Extractor | None = None,
        reply_engine: ReplyEngine | None = None,
        reply_provider: ReplyProvider | None = None,
        reply_guard: ReplyGuard | None = None,
        sink: JsonlSink | None = None,
        sender: ReplySender | None = None,
        clock: Clock | None = None,
        on_result: Callable[[ProcessResult], None] | None = None,
        latency_store: LatencyStore | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.dedup = dedup or Deduplicator(clock=self.clock)
        self.classifier = classifier or Classifier()
        self.extractor = extractor or Extractor()
        self.reply_engine = reply_engine or ReplyEngine(clock=self.clock)
        self.reply_provider = reply_provider or RuleReplyProvider(self.reply_engine)
        self.reply_guard = reply_guard or ReplyGuard(clock=self.clock)
        self.sink = sink
        self.sender = sender
        self.on_result = on_result
        self.latency_store = latency_store

    def handle_event(self, event: Event) -> ProcessResult | None:
        """作为适配器回调使用；非消息事件会被忽略。"""
        if event.event_type is not EventType.MESSAGE or event.message is None:
            return None
        # Event.timestamp 视为适配器检出时间。
        session_open = None
        if event.extra:
            if "session_open" in event.extra:
                session_open = bool(event.extra["session_open"])
        return self.process(event.message, detected_at=event.timestamp, session_open=session_open)

    def process(
        self,
        message: Message,
        *,
        detected_at: datetime | None = None,
        session_open: bool | None = None,
    ) -> ProcessResult:
        now = self.clock.now()
        detected = detected_at or now
        if session_open is None and message.extra:
            if "session_open" in message.extra:
                session_open = bool(message.extra["session_open"])
        preview = (message.content or "")[:80]
        trace = LatencyTrace(
            message_id=message.message_id,
            group_id=message.group_id,
            kind="auto_reply",
            session_open=session_open,
            preview=preview,
            message_at=message.timestamp if isinstance(message.timestamp, datetime) else None,
            detected_at=detected,
        )
        if self.latency_store is not None:
            self.latency_store.remember(trace)

        decision_started = time.perf_counter()
        if self.dedup.is_duplicate(message):
            trace.decided_at = self.clock.now()
            trace.decision_s = time.perf_counter() - decision_started
            trace.outcome = "duplicate"
            result = ProcessResult(
                message=message,
                duplicate=True,
                category=None,
                extraction=None,
                reply_id=None,
                reply_text=None,
                record=self._record(message, duplicate=True),
                skipped=True,
                trace=trace,
            )
            self._emit(result)
            self._flush_trace_if_idle(trace)
            return result

        extraction = self.extractor.extract(message)
        category = self.classifier.classify(message)
        trace.classified_at = self.clock.now()
        skip_reason: str | None = None
        reply_id = None
        reply_text = None
        queued = False
        guard: GuardDecision = self.reply_guard.inspect(message)
        if not guard.allow:
            skip_reason = guard.reason
        else:
            proposal: ReplyProposal | None = self.reply_provider.propose(
                message,
                ReplyContext(category=category, extraction=extraction),
            )
            if proposal is not None:
                reply_id = proposal.rule_id or proposal.source
                reply_text = proposal.text
                if self.sender is not None:
                    self._submit_reply(message.group_id, proposal.text, trace)
                    queued = True
                self.reply_guard.record_reply(message.group_id, self.clock.now())

        trace.decided_at = self.clock.now()
        trace.decision_s = time.perf_counter() - decision_started
        if not queued:
            trace.outcome = "no_send" if not reply_text else "queued_skipped"

        result = ProcessResult(
            message=message,
            duplicate=False,
            category=category,
            extraction=extraction,
            reply_id=reply_id,
            reply_text=reply_text,
            record=self._record(
                message,
                duplicate=False,
                category=category,
                extraction=extraction,
                reply_id=reply_id,
                reply_text=reply_text,
                skip_reason=skip_reason,
            ),
            skip_reason=skip_reason,
            trace=trace,
        )
        self._emit(result)
        if not queued:
            self._flush_trace_if_idle(trace)
        return result

    def _submit_reply(self, group_id: str, text: str, trace: LatencyTrace) -> None:
        """向发送器提交自动回复；兼容不接受 trace 参数的实现。"""
        sender = self.sender
        if sender is None:
            return
        try:
            sender.submit_auto_reply(group_id, text, trace=trace)
        except TypeError:
            sender.submit_auto_reply(group_id, text)  # type: ignore[call-arg]

    def _flush_trace_if_idle(self, trace: LatencyTrace) -> None:
        if self.latency_store is not None:
            self.latency_store.write(trace)

    def _record(
        self,
        message: Message,
        *,
        duplicate: bool,
        category: Category | None = None,
        extraction: Extraction | None = None,
        reply_id: str | None = None,
        reply_text: str | None = None,
        skip_reason: str | None = None,
    ) -> dict:
        return {
            "processed_at": self.clock.now().isoformat(timespec="seconds"),
            "message_id": message.message_id,
            "group_id": message.group_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "content": message.content,
            "timestamp": message.timestamp.isoformat(timespec="seconds")
            if isinstance(message.timestamp, datetime)
            else str(message.timestamp),
            "duplicate": duplicate,
            "category": category.value if category else None,
            "extraction": extraction.to_dict() if extraction else None,
            "reply_id": reply_id,
            "reply_text": reply_text,
            "skip_reason": skip_reason,
        }

    def _emit(self, result: ProcessResult) -> None:
        if self.sink is not None:
            self.sink.write(result.record)
        if self.on_result is not None:
            self.on_result(result)

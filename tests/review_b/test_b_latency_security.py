# -*- coding: utf-8 -*-
"""分段耗时打点完整性、日志脱敏、无硬编码密钥、禁止功能扫描。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from src.adapters.ui_driver import FakeUiDriver
from src.clock import FakeClock
from src.latency import LatencyStore, LatencyTrace
from src.logging_util import redact
from src.ratelimit.bucket import BucketSpec, RateLimitConfig, RateLimiter
from src.ratelimit.sender import HumanizeConfig, HumanizedSender, NightSilentConfig

from constants import FORBIDDEN_FEATURE_KEYWORDS, FORBIDDEN_IDENTIFIERS, PROHIBITION_HINTS
from review_b.helpers import make_adapter

REPO = Path(__file__).resolve().parents[2]

B_FILES = [
    REPO / "src" / "adapters" / "uia.py",
    REPO / "src" / "adapters" / "ui_driver.py",
    REPO / "src" / "adapters" / "pyweixin_driver.py",
    REPO / "src" / "adapters" / "pause.py",
    REPO / "scripts" / "selfcheck_uia.py",
    REPO / "docs" / "ADAPTER_B.md",
]

TIMING_FIELDS = (
    "open_chat",
    "paste",
    "send",
    "verify",
    "input_pause",
    "group_switch_wait",
    "preheat",
    "adapter_total",
    "chat_was_open",
)

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|authorization|token)\s*=\s*['\"][^'\"]{8,}['\"]"
)
_PLACEHOLDER = re.compile(r"(?i)(your[_-]?|changeme|placeholder|example|xxx|\*\*\*|dummy|fake|redact)")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_WXID_RE = re.compile(r"wxid_[a-zA-Z0-9_-]+", re.IGNORECASE)
_HOOK_RE = re.compile(r"(?i)\b(inline hook|dll inject|process inject|frida|gdb hook)\b")


def test_adapter_result_timings_fields_are_complete(tmp_path: Path):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)
    driver.open_chat("交易会员群-主群")
    result = adapter.send("g1", "打点")
    assert result.ok
    data = result.timings.as_dict()
    missing = [name for name in TIMING_FIELDS if name not in data]
    assert missing == [], f"UiaSendResult.timings 缺字段: {missing}"
    assert data["chat_was_open"] is True
    assert data["adapter_total"] >= 0


def test_latency_jsonl_from_official_path_misses_uia_stage_fields(tmp_path: Path):
    """HumanizedSender 只写 adapter_send_s，不把 UIA 分段字段和 chat_was_open 写入 latency.jsonl。"""
    clock = FakeClock()
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver, clock=clock)
    driver.open_chat("交易会员群-主群")
    store = LatencyStore(tmp_path / "latency.jsonl")
    sender = HumanizedSender(
        adapter,
        RateLimiter(
            RateLimitConfig(
                global_bucket=BucketSpec(8, 0.25),
                per_group=BucketSpec(3, 0.1),
                announcement=BucketSpec(4, 0.1),
                auto_reply=BucketSpec(6, 0.2),
                global_replies_per_hour=40,
            ),
            clock=clock,
        ),
        config=HumanizeConfig(
            send_delay_min=0.0,
            send_delay_max=0.0,
            typing_hook=False,
            night=NightSilentConfig(enabled=False),
        ),
        clock=clock,
        latency_store=store,
    )
    trace = LatencyTrace(message_id="lat-1", group_id="g1", kind="auto_reply")
    sender.submit_auto_reply("g1", "需要完整分段", trace=trace)
    assert sender.pump(max_items=1) == 1
    assert adapter.last_result is not None
    rows = store.read_all()
    assert rows, "正式出口应写入 latency.jsonl"
    row = rows[-1]
    extra = row.get("extra") or {}
    segments = row.get("segments") or {}
    # 正确行为：应带上 open_chat/paste/send/verify/chat_was_open（或 session_open）
    has_stages = all(key in extra or key in segments or key in row for key in ("open_chat", "paste", "send", "verify"))
    session = row.get("session_open")
    if session is None:
        session = extra.get("chat_was_open")
    assert has_stages, (
        f"latency.jsonl 缺少 UIA 分段字段 open_chat/paste/send/verify；"
        f"实际 keys={list(row.keys())} segments={list(segments.keys())} extra={extra}"
    )
    assert session is True, f"已打开会话发送后 session_open/chat_was_open 应为 True，实际 {session}"


def test_uia_alerts_and_logs_redact_phone_and_token(tmp_path: Path, caplog):
    driver = FakeUiDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=driver)

    from src.adapters.ui_driver import UiNotReadyError

    class LeakyDriver(FakeUiDriver):
        def is_locked(self) -> bool:
            raise UiNotReadyError("锁屏联系 13800138000 token=abcdEFGH123456 wxid_abcde123")

    leaky = LeakyDriver(known_chats={"交易会员群-主群"})
    adapter = make_adapter(tmp_path, driver=leaky)
    with caplog.at_level(logging.WARNING):
        result = adapter.send("g1", "告警脱敏")
    assert not result.ok
    alert_path = tmp_path / "data" / "uia_alerts.jsonl"
    assert alert_path.exists()
    raw = alert_path.read_text(encoding="utf-8")
    assert "13800138000" not in raw, "uia_alerts.jsonl 未脱敏手机号"
    assert "abcdEFGH123456" not in raw, "uia_alerts.jsonl 未脱敏 token"
    assert "wxid_abcde123" not in raw, "uia_alerts.jsonl 未脱敏 wxid"
    joined = " ".join(r.getMessage() for r in caplog.records)
    # 日志路径在 uia.py 对 message 做了 redact
    assert "13800138000" not in joined
    assert "wxid_abcde123" not in joined


def test_pyweixin_logger_calls_are_not_all_redacted():
    """pyweixin_driver 的 logger.warning 直接写入异常文本，且该 logger 未挂 RedactFilter。"""
    src = (REPO / "src" / "adapters" / "pyweixin_driver.py").read_text(encoding="utf-8")
    assert "from src.logging_util import redact" not in src
    # 存在未脱敏的 logger 调用
    unredacted = []
    for i, line in enumerate(src.splitlines(), 1):
        if "logger." in line and "redact(" not in line and "debug" not in line:
            if "%s" in line or "{" in line:
                unredacted.append(f"{i}:{line.strip()}")
    assert unredacted == [], "pyweixin_driver 存在未调用 redact 的日志输出:\n" + "\n".join(unredacted)


def test_no_hardcoded_secrets_in_b_sources():
    hits = []
    for path in B_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if _SECRET_ASSIGN_RE.search(line) and not _PLACEHOLDER.search(line):
                hits.append(f"{path.relative_to(REPO)}:{i}: {stripped[:160]}")
    assert hits == [], "发现疑似硬编码密钥:\n" + "\n".join(hits)


def test_b_sources_do_not_embed_phone_or_wxid_literals():
    hits = []
    for path in B_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _PHONE_RE.search(line) or _WXID_RE.search(line):
                hits.append(f"{path.relative_to(REPO)}:{i}: {line.strip()[:160]}")
    assert hits == [], "适配器 B 源码或文档中出现手机号/wxid 字面量:\n" + "\n".join(hits)


def test_no_forbidden_feature_implementations():
    ident_hits = []
    keyword_hits = []
    for path in B_FILES:
        if path.suffix == ".md":
            # 文档允许在「严禁」语境出现关键词；标识符仍不允许
            text = path.read_text(encoding="utf-8")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            lower = stripped.lower()
            for ident in FORBIDDEN_IDENTIFIERS:
                if re.search(rf"\b{re.escape(ident)}\b", lower):
                    ident_hits.append(f"{path.relative_to(REPO)}:{i}: {stripped[:160]}")
            if path.suffix == ".md":
                continue
            if any(h in stripped for h in PROHIBITION_HINTS):
                continue
            for kw in FORBIDDEN_FEATURE_KEYWORDS:
                if kw.lower() in lower:
                    keyword_hits.append(f"{path.relative_to(REPO)}:{i}: {kw}: {stripped[:160]}")
    assert ident_hits == [], "发现禁止功能标识符:\n" + "\n".join(ident_hits)
    assert keyword_hits == [], "发现非禁止语境的禁止功能关键词:\n" + "\n".join(keyword_hits)


def test_no_hook_or_protocol_implementation_in_b():
    hits = []
    for path in B_FILES:
        if path.suffix == ".md":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _HOOK_RE.search(line):
                hits.append(f"{path.relative_to(REPO)}:{i}: {line.strip()[:160]}")
    assert hits == [], "适配器 B 实现中出现 Hook/注入痕迹:\n" + "\n".join(hits)


def test_redact_helper_covers_phone_wxid_token():
    sample = "联系 13912345678 微信号: testuser token=superSecretValue wxid_deadbeef01"
    out = redact(sample)
    assert "13912345678" not in out
    assert "superSecretValue" not in out
    assert "wxid_deadbeef01" not in out

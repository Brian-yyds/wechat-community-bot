# -*- coding: utf-8 -*-
"""安全与脱敏：日志不泄露微信号/手机号/token；禁止功能关键词扫描。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.logging_util import RedactFilter, redact, setup_logging
from src.run_mode import SendGate
from src.safety import find_forbidden

from .helpers import REPO_ROOT, attach_logger, make_sender
from src.clock import FakeClock

FORBIDDEN_TERMS = (
    "博彩",
    "六合彩",
    "下注",
    "投注",
    "开奖",
    "彩票",
    "赔率",
    "盘口",
    "赌球",
    "赌场",
    "结算",
    "收款",
    "代收",
    "收付款",
)

# 允许出现在注释、拒绝文案、安全模块中的「严禁」语境；不得作为功能实现标识。
_IMPL_NAME_RE = re.compile(
    r"(def|class)\s+\w*(lottery|wager|payout|payment_collect|settle_bet|gambling|liuhe)\w*",
    re.IGNORECASE,
)


def test_redact_phone_wxid_token_wechat_name():
    text = "联系 13800138000，wxid_abc123xyz，微信号: alice_wx，token=abcdEFGH1234，Bearer abcdefghijklmnop"
    out = redact(text)
    assert "13800138000" not in out
    assert "wxid_abc123xyz" not in out
    assert "alice_wx" not in out
    assert "abcdEFGH1234" not in out
    assert "abcdefghijklmnop" not in out
    assert "****" in out or "***" in out


def test_send_gate_drop_log_is_redacted(tmp_path: Path, clock: FakeClock):
    logger, handler = attach_logger("wechat_bot.review_e.sec")
    sender, _, _, _ = make_sender(clock, tmp_path, "observe", logger=logger)
    sender.submit_auto_reply("wxid_hiddenid01", "正文里有 13700001111 和 token=zzzsecret999")
    sender.pump()
    joined = "\n".join(handler.records)
    assert "wxid_hiddenid01" not in joined
    assert "13700001111" not in joined
    assert "zzzsecret999" not in joined
    assert "正文里有" not in joined  # 丢弃日志不应写入待发正文


def test_setup_logging_redacts_args_without_breaking_percent_format():
    """脱敏过滤器不得把模板里的 token=%s 先替换成 token=***，否则 getMessage 会 TypeError。"""
    logger = logging.getLogger("wechat_bot.review_e.setup")
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.INFO)
    logger.addFilter(RedactFilter())
    handler_msgs: list[str] = []
    errors: list[BaseException] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                handler_msgs.append(record.getMessage())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    h = _H()
    h.addFilter(RedactFilter())
    logger.addHandler(h)
    logger.propagate = False
    logger.info("user=%s token=%s phone=%s", "wxid_someone01", "tok_secret_value", "13900001111")
    assert not errors, (
        f"RedactFilter 破坏了日志格式化: {errors!r}。"
        "它对 msg 模板里的 token=%s 先做了赋值脱敏，导致 %s 数量与 args 不一致。"
    )
    joined = "\n".join(handler_msgs)
    assert "wxid_someone01" not in joined
    assert "tok_secret_value" not in joined
    assert "13900001111" not in joined


def test_safety_hits_forbidden_keywords():
    for term in FORBIDDEN_TERMS:
        assert find_forbidden(f"请处理{term}事宜") == term
    assert find_forbidden("请注意风险控制") is None


def test_module_e_sources_have_no_forbidden_feature_implementation():
    """扫描模块 E 相关源码：只允许安全拦截/注释中的违禁词，不得出现功能实现。"""
    files = [
        REPO_ROOT / "src" / "run_mode.py",
        REPO_ROOT / "src" / "latency.py",
        REPO_ROOT / "src" / "main.py",
        REPO_ROOT / "src" / "ratelimit" / "sender.py",
        REPO_ROOT / "src" / "ratelimit" / "bucket.py",
        REPO_ROOT / "src" / "adapters" / "pause.py",
    ]
    impl_hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        if _IMPL_NAME_RE.search(text):
            impl_hits.append(f"{rel}: 可疑功能标识符")
        # 若作为可调用业务接口出现（非注释、非「严禁」说明）则失败。
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if "严禁" in stripped or "不提供" in stripped or "拒绝" in stripped or "命中" in stripped:
                continue
            if stripped.startswith("print(") or "严禁：" in stripped:
                continue
            for term in ("下注接口", "开奖接口", "代收接口", "结算接口", "收款功能"):
                if term in stripped:
                    impl_hits.append(f"{rel}:{i}: {stripped}")
    assert not impl_hits, impl_hits


def test_repo_src_has_no_gambling_or_payment_modules():
    bad = []
    for path in (REPO_ROOT / "src").rglob("*"):
        if path.suffix in {".pyc"} or path.name == "__pycache__":
            continue
        name = path.name.lower()
        for needle in ("lottery", "gambling", "payout", "betting", "liuhe", "bocai"):
            if needle in name:
                bad.append(str(path.relative_to(REPO_ROOT)))
    assert not bad, bad

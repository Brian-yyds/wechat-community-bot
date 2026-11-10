# -*- coding: utf-8 -*-
"""测试适配层：对接开发交付的模块；模块不存在时由用例 skip，而不是当框架错误。

开发请实现下列约定（任选其一即可被自动发现）：

1. 环境变量 BOT_ADAPTER_MODULE 指向的模块，例如 src.bot_api
2. 或默认候选：src.bot_api / src.api / src.bot

模块需提供的接口
================

分类
----
CATEGORIES = ["提问", "行情讨论", "广告刷屏", "公告回复", "闲聊/无关", "需人工"]
# 负向类使用 "禁止功能-上报"

def classify(message: dict) -> dict:
    \"\"\"message 至少包含 id, group, sender, ts, text，可选 quote / mentions。
    返回：
      category: str
      should_reply: bool
      reply: str          # 不回复时为空字符串
      flags: list[str]    # 如 spam / need_human / forbidden_report
    \"\"\"

调度（可注入时钟）
------------------
class Scheduler:
    def __init__(self, clock=None, store=None, catch_up_window_seconds=600): ...
    def set_clock(self, clock) -> None: ...          # clock.now() -> datetime
    def add_announcement(self, job_id, content, run_at, group) -> None: ...
    def tick(self, now=None) -> list:                # 到期且尚未发送的任务
    def mark_sent(self, job_id, sent_at) -> None: ...
    def on_recover(self, now=None) -> list:          # 掉线/重启补发，不得重复

限流与拟人化
------------
class RateLimiter:
    def allow(self, *, group, content_hash, now, kind="reply") -> bool: ...
    def record(self, *, group, content_hash, now, kind="reply") -> None: ...
    def next_delay(self) -> float:                   # 秒，含抖动
    def in_night_silence(self, now) -> bool: ...

def humanize_delay_range() -> tuple[float, float]:
    \"\"\"返回 (下限秒, 上限秒)，须满足 1.5 ≤ 下限 < 上限 ≤ 8，且与 P95≤12s 相容。\"\"\"

发送层（测试将注入或替换 sender）
--------------------------------
class Sender:
    def send(self, group, text, *, kind="reply") -> object: ...

日志脱敏
--------
def redact_log(line: str) -> str:
    \"\"\"对手机号、微信号、wxid、token 做掩码。\"\"\"

脚手架自检时设置 BOT_TEST_USE_MOCK=1，将加载 tests/mock（不是产品实现，禁止部署）。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class AdapterNotReady(Exception):
    """开发模块尚未交付或接口不完整。测试应 pytest.skip，而不是 fail。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def use_mock() -> bool:
    return os.environ.get("BOT_TEST_USE_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}


def _load_module():
    if use_mock():
        from mock import impl  # 仅测试脚手架

        return impl

    named = os.environ.get("BOT_ADAPTER_MODULE", "").strip()
    candidates = [named] if named else ["src.bot_api", "src.api", "src.bot", "bot_api"]
    errors = []
    for name in candidates:
        if not name:
            continue
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
            continue
    raise AdapterNotReady(
        "未找到开发交付的模块。请实现 tests/README.md 中的约定接口，"
        "将模块放到 src/bot_api.py（或设置 BOT_ADAPTER_MODULE）。"
        "若只想跑脚手架自检：BOT_TEST_USE_MOCK=1。"
        "尝试记录: " + " | ".join(errors)
    )


_impl = None
_impl_error = None


def get_impl():
    global _impl, _impl_error
    if _impl is not None:
        return _impl
    if _impl_error is not None:
        raise AdapterNotReady(_impl_error)
    try:
        _impl = _load_module()
        return _impl
    except AdapterNotReady as exc:
        _impl_error = exc.reason
        raise


def require_attr(*names):
    """返回实现模块，若缺属性则 AdapterNotReady。"""
    impl = get_impl()
    missing = [n for n in names if not hasattr(impl, n)]
    if missing:
        raise AdapterNotReady(
            "模块已找到，但缺少接口: " + ", ".join(missing) + "。请对照 tests/README.md 补齐。"
        )
    return impl

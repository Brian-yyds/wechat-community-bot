"""违禁内容护栏。

加载回复规则与定时公告配置时会扫描文案；一旦命中则拒绝启动。
下列词语只用于拦截配置，严禁作为业务功能实现。
"""

from __future__ import annotations

import re
from typing import Iterable

# 每一行都带「严禁」上下文，仅作拒绝加载用，严禁实现这些功能。
_FORBIDDEN_TERMS = (
    "博彩",  # 严禁实现此类功能
    "六合彩",  # 严禁实现此类功能
    "下注",  # 严禁实现此类功能
    "投注",  # 严禁实现此类功能
    "开奖",  # 严禁实现此类功能
    "彩票",  # 严禁实现此类功能
    "赔率",  # 严禁实现此类功能
    "盘口",  # 严禁实现此类功能
    "赌球",  # 严禁实现此类功能
    "赌场",  # 严禁实现此类功能
    "结算",  # 严禁作为本机器人的业务功能
    "收款",  # 严禁作为本机器人的业务功能
    "代收",  # 严禁实现此类功能
    "收付款",  # 严禁实现此类功能
)


class ForbiddenContentError(ValueError):
    """配置或规则中出现了不被允许的业务内容。"""


_FORBIDDEN_RE = re.compile("|".join(re.escape(t) for t in _FORBIDDEN_TERMS))


def find_forbidden(text: str | None) -> str | None:
    """若文本包含违禁词，返回命中的词；否则返回 None。"""
    if not text:
        return None
    match = _FORBIDDEN_RE.search(text)
    return match.group(0) if match else None


def check_forbidden(text: str | None, where: str) -> None:
    """检查单段文本，命中则抛出 ForbiddenContentError。"""
    hit = find_forbidden(text)
    if hit:
        raise ForbiddenContentError(
            f"发现违禁内容（{where}，命中「{hit}」）。"
            "本项目不提供被禁止的业务功能或规则，已停止加载。"
        )


def check_forbidden_many(parts: Iterable[tuple[str, str]]) -> None:
    """批量检查：(文本, 位置说明)。"""
    for text, where in parts:
        check_forbidden(text, where)

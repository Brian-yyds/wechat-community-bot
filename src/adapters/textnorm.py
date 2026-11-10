"""UI 文本规范化：NFKC、去空白、大小写折叠后做严格精确匹配。

适配器 B 的会话标题与适配器 D 的 OCR 文案使用同一套规则，避免全半角差异误判。
比较必须在规范化后完全相等，禁止把同名前缀或子串当成匹配。

OCR 模块目前仍保留一份同名实现，本文件先供 B 使用，以免改动正在并行修改的 ocr.py。
"""

from __future__ import annotations

import unicodedata


def normalize_ui_text(text: str | None) -> str:
    """兼容字符、全半角、空白与大小写后再比较。"""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(ch for ch in folded if not ch.isspace())


def texts_match_exactly(left: str | None, right: str | None) -> bool:
    """规范化后完全相等才算匹配，禁止子串互相包含。"""
    a = normalize_ui_text(left)
    b = normalize_ui_text(right)
    return bool(a) and a == b

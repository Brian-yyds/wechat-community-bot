# -*- coding: utf-8 -*-
"""C/D 安全与脱敏扫描：密钥、日志泄露、禁止功能关键词。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from constants import FORBIDDEN_FEATURE_KEYWORDS, FORBIDDEN_IDENTIFIERS, PROHIBITION_HINTS

REPO = Path(__file__).resolve().parents[2]

CD_FILES = [
    REPO / "src" / "adapters" / "notification.py",
    REPO / "src" / "adapters" / "ocr.py",
    REPO / "src" / "adapters" / "pause.py",
    REPO / "src" / "adapters" / "factory.py",
    REPO / "src" / "adapters" / "composite.py",
    REPO / "src" / "adapters" / "roles.py",
    REPO / "scripts" / "selfcheck_notification.py",
    REPO / "scripts" / "selfcheck_ocr.py",
    REPO / "docs" / "ADAPTER_C.md",
    REPO / "docs" / "ADAPTER_D.md",
]

# 赋值形式的硬编码密钥（排除注释里的环境变量名与占位符）。
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|authorization|token)\s*=\s*['\"][^'\"]{8,}['\"]"
)
_PLACEHOLDER = re.compile(r"(?i)(your[_-]?|changeme|placeholder|example|xxx|\*\*\*|dummy|fake|redact)")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_WXID_RE = re.compile(r"wxid_[a-zA-Z0-9_-]+", re.IGNORECASE)


def _is_prohibition_context(line: str) -> bool:
    return any(h in line for h in PROHIBITION_HINTS)


def test_cd_files_exist():
    missing = [str(p) for p in CD_FILES if not p.exists()]
    assert missing == [], f"C/D 核查范围内文件缺失: {missing}"


def test_no_hardcoded_secrets_in_cd_sources():
    hits = []
    for path in CD_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if _SECRET_ASSIGN_RE.search(line) and not _PLACEHOLDER.search(line):
                hits.append(f"{path.relative_to(REPO)}:{i}: {stripped[:160]}")
    assert hits == [], "发现疑似硬编码密钥:\n" + "\n".join(hits)


def test_cd_sources_do_not_embed_phone_or_wxid_literals():
    hits = []
    for path in CD_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _PHONE_RE.search(line) or _WXID_RE.search(line):
                hits.append(f"{path.relative_to(REPO)}:{i}: {line.strip()[:160]}")
    assert hits == [], "C/D 源码或文档中出现手机号/wxid 字面量:\n" + "\n".join(hits)


def test_cd_adapters_do_not_log_unredacted_and_have_no_screenshot_persist():
    # 适配器实现本身未接 logging，也未把截图写入磁盘；若以后补日志必须走脱敏。
    persist_hits = []
    log_hits = []
    persist_re = re.compile(
        r"NamedTemporaryFile|TemporaryDirectory|mkstemp|screenshot\(\).*\.save|tobytes|imwrite|\.png|\.jpg",
        re.IGNORECASE,
    )
    for rel in ("src/adapters/notification.py", "src/adapters/ocr.py", "src/adapters/pause.py"):
        path = REPO / rel
        text = path.read_text(encoding="utf-8")
        if "import logging" in text or "getLogger" in text:
            if "redact" not in text and "RedactFilter" not in text:
                log_hits.append(f"{rel} 使用了 logging 但未见脱敏")
        for i, line in enumerate(text.splitlines(), 1):
            if persist_re.search(line) and "fake-screenshot" not in line and "docs/" not in line:
                # 允许注释中提到截图，不允许实际落盘 API。
                if line.strip().startswith("#") or line.strip().startswith('"""') or line.strip().startswith("本"):
                    continue
                if "未验证" in line or "截图" in line and ("→" in line or "->" in line or "不会" in line):
                    continue
                persist_hits.append(f"{rel}:{i}: {line.strip()[:160]}")
    assert log_hits == [], "\n".join(log_hits)
    # 当前骨架不应把截图写到文件；若新增落盘，必须同时有清理逻辑。
    dangerous = [h for h in persist_hits if any(k in h.lower() for k in ("namedtemporary", "mkstemp", "imwrite", ".save", ".png", ".jpg"))]
    assert dangerous == [], "疑似截图/临时文件落盘且未见清理约定:\n" + "\n".join(dangerous)


def test_cd_sources_have_no_forbidden_feature_implementation():
    keyword_hits = []
    ident_hits = []
    for path in CD_FILES:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for i, line in enumerate(lines, 1):
            for ident in FORBIDDEN_IDENTIFIERS:
                if re.search(r"\b" + re.escape(ident) + r"\b", line, re.I):
                    ident_hits.append(f"{path.relative_to(REPO)}:{i}: {ident} :: {line.strip()[:160]}")
            if _is_prohibition_context(line):
                continue
            lower = line.lower()
            for kw in FORBIDDEN_FEATURE_KEYWORDS:
                if kw.lower() in lower:
                    keyword_hits.append(f"{path.relative_to(REPO)}:{i}: {kw} :: {line.strip()[:160]}")
    assert ident_hits == [], "C/D 范围出现禁止功能标识符:\n" + "\n".join(ident_hits)
    assert keyword_hits == [], "C/D 范围出现禁止功能关键词（非「严禁」声明）:\n" + "\n".join(keyword_hits)

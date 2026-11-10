# -*- coding: utf-8 -*-
"""禁止功能扫描（FB-01～FB-04）：代码、配置、回复/公告模板中不得出现博彩/结算/收款等实现。

扫描范围排除 tests/ 与 .git/。文档中的「严禁/不得」声明不作为失败。
本文件不依赖开发模块，可对已有目录直接跑。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from constants import FORBIDDEN_FEATURE_KEYWORDS, FORBIDDEN_IDENTIFIERS, PROHIBITION_HINTS

SCAN_DIR_NAMES = ("src", "config", "configs", "templates", "template", "conf")
SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules", "tests"}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".go", ".java", ".json", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".txt", ".md", ".j2", ".jinja", ".html",
    ".xml", ".sh", ".env.example",
}


def _is_prohibition_context(line: str) -> bool:
    return any(h in line for h in PROHIBITION_HINTS)


def _iter_scan_files(repo_root: Path):
    # 只扫约定的产品目录，避免把根 README 的禁止声明或测试资产算进去
    for name in SCAN_DIR_NAMES:
        d = repo_root / name
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"Makefile", "Dockerfile"}:
                continue
            if p.stat().st_size > 2_000_000:
                continue
            yield p


# 用于拦截违禁配置的护栏模块（如 src/safety.py）允许出现关键词表，但不得出现功能标识符。
_GUARD_HINTS = (
    "check_forbidden",
    "find_forbidden",
    "ForbiddenContent",
    "违禁内容",
    "_FORBIDDEN_TERMS",
    "不提供博彩",
)


def _is_guard_module(text: str) -> bool:
    return sum(1 for h in _GUARD_HINTS if h in text) >= 2


def _scan_file(path: Path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    keyword_hits = []
    ident_hits = []
    guard = _is_guard_module(text)
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        lower = line.lower()
        for ident in FORBIDDEN_IDENTIFIERS:
            if re.search(r"\b" + re.escape(ident) + r"\b", line, re.I):
                ident_hits.append((i, ident, line.strip()[:200]))
        if guard or _is_prohibition_context(line):
            continue
        for kw in FORBIDDEN_FEATURE_KEYWORDS:
            if kw.lower() in lower:
                keyword_hits.append((i, kw, line.strip()[:200]))
    return keyword_hits, ident_hits


def test_fb01_fb03_source_config_templates_have_no_forbidden_features(repo_root):
    """FB-01/02/03 源码、配置、模板中不得出现禁止功能（禁令声明除外）。"""
    scanned = list(_iter_scan_files(repo_root))
    keyword_findings = []
    ident_findings = []
    for p in scanned:
        k, ids = _scan_file(p)
        rel = str(p.relative_to(repo_root))
        for item in k:
            keyword_findings.append((rel, *item))
        for item in ids:
            ident_findings.append((rel, *item))

    # 空 src 时：没有命中即通过（表示当前未见实现迹象）
    msg = []
    if ident_findings:
        msg.append("发现疑似禁止功能标识（阻断）:")
        for rel, ln, ident, snippet in ident_findings:
            msg.append(f"  {rel}:{ln}  [{ident}] {snippet}")
    if keyword_findings:
        msg.append("发现禁止功能关键词（非「严禁」上下文，阻断）:")
        for rel, ln, kw, snippet in keyword_findings:
            msg.append(f"  {rel}:{ln}  [{kw}] {snippet}")
    assert not ident_findings and not keyword_findings, "\n".join(msg)


def test_fb04_no_forbidden_identifiers_in_filenames(repo_root):
    """FB-04 文件名/模块名不得体现下注、结算、收款等能力。"""
    bad = []
    for name in SCAN_DIR_NAMES:
        d = repo_root / name
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if any(part in SKIP_PARTS for part in p.parts):
                continue
            low = p.name.lower()
            for ident in FORBIDDEN_IDENTIFIERS:
                if ident.lower() in low:
                    bad.append((str(p.relative_to(repo_root)), ident))
    assert not bad, "文件名含禁止功能标识: " + ", ".join(f"{p}[{i}]" for p, i in bad)


def test_scan_scope_does_not_require_src_to_exist(repo_root):
    """即使 src 为空也应能完成扫描（本用例保证脚手架可跑）。"""
    files = list(_iter_scan_files(repo_root))
    assert isinstance(files, list)

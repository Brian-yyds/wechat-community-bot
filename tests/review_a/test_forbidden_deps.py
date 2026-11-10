# -*- coding: utf-8 -*-
"""禁止功能关键词扫描、依赖与权限检查（适配器 A 及相关产品目录）。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from constants import FORBIDDEN_FEATURE_KEYWORDS, FORBIDDEN_IDENTIFIERS, PROHIBITION_HINTS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "config")
A_FOCUS = [
    REPO_ROOT / "src" / "adapters" / "local_db.py",
    REPO_ROOT / "src" / "adapters" / "local_capture.py",
    REPO_ROOT / "docs" / "ADAPTER_A.md",
]
HOOK_IMPL_RE = re.compile(
    r"\b(frida|pymem|ctypes\.windll|WriteProcessMemory|OpenProcess|inject_dll|"
    r"ptrace| mandelhook|EasyHook|MinHook)\b",
    re.I,
)
# 协议/Hook 实现特征；文档里的「不做 Hook/协议」声明单独处理。
HOOK_WORD_RE = re.compile(r"\b(hook|iPad协议|ipa[dD]\s*protocol|dll注入|进程注入)\b", re.I)


def _is_prohibition(line: str) -> bool:
    return any(h in line for h in PROHIBITION_HINTS) or "不注入" in line or "不 Hook" in line or "不做 Hook" in line


def _iter_product_files():
    suffixes = {".py", ".json", ".yml", ".yaml", ".toml", ".md", ".txt", ".ini"}
    for name in SCAN_DIRS:
        d = REPO_ROOT / name
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in suffixes:
                continue
            if any(part in {".git", ".venv", "__pycache__", "tests"} for part in p.parts):
                continue
            yield p


def test_no_forbidden_feature_identifiers_in_product_code():
    hits = []
    for path in _iter_product_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            for ident in FORBIDDEN_IDENTIFIERS:
                if re.search(r"\b" + re.escape(ident) + r"\b", line, re.I):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{i}: {ident} :: {line.strip()[:160]}")
    assert not hits, "发现禁止功能标识符:\n" + "\n".join(hits)


def test_no_forbidden_keywords_outside_prohibition_context_in_adapter_a():
    hits = []
    for path in A_FOCUS:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _is_prohibition(line):
                continue
            lower = line.lower()
            for kw in FORBIDDEN_FEATURE_KEYWORDS:
                if kw.lower() in lower:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{i}: {kw} :: {line.strip()[:160]}")
    assert not hits, "适配器 A 出现禁止功能关键词（且非严禁声明）:\n" + "\n".join(hits)


def test_adapter_a_has_no_hook_or_protocol_implementation():
    hits = []
    for path in A_FOCUS:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if _is_prohibition(line) or line.strip().startswith("#") or line.strip().startswith("-"):
                # 文档条目与注释中的风险自报允许提到 Hook / 协议
                continue
            if "不内置" in line or "不是零风险" in line or "小于 Hook" in line:
                continue
            if HOOK_IMPL_RE.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()[:160]}")
    assert not hits, "适配器 A 疑似包含 Hook/注入实现:\n" + "\n".join(hits)


def test_requirements_have_no_offensive_or_decrypt_deps():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    banned = ("frida", "pymem", "pyinjector", "sqlcipher", "pysqlcipher", "wxdump", "pywxdump", "hook")
    hits = [name for name in banned if name in req]
    assert not hits, f"requirements.txt 出现不应出现的依赖: {hits}"
    # 适配器 A 只应依赖标准库 sqlite3
    allowed_top = {"pytest", "pyyaml", "yaml"}
    for line in req.splitlines():
        raw = line.split("#")[0].strip()
        if not raw:
            continue
        name = re.split(r"[<>=~\[]", raw, maxsplit=1)[0].strip()
        # 项目当前依赖极简，不应偷偷加入解密/Hook 库
        assert name.lower() in allowed_top or name.lower() in {"pytest-timeout"}, f"未知依赖 {name}"


def test_adapter_a_imports_stay_in_stdlib_and_project(tmp_path: Path):
    for rel in ("src/adapters/local_db.py", "src/adapters/local_capture.py"):
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module.split(".")[0])
        unexpected = [
            m
            for m in mods
            if m
            not in {
                "__future__",
                "abc",
                "asyncio",
                "collections",
                "dataclasses",
                "datetime",
                "json",
                "logging",
                "os",
                "pathlib",
                "queue",
                "random",
                "re",
                "sqlite3",
                "threading",
                "time",
                "typing",
                "urllib",
                "src",
            }
        ]
        assert not unexpected, f"{rel} 引入了意外依赖: {unexpected}"


def test_no_setuid_or_admin_manifest_in_adapter_a():
    for path in A_FOCUS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for token in ("requireadministrator", "setuid", "sudo ", "runas ", "se_debug"):
            # 文档若写「不要用管理员权限」属于禁止声明，允许
            if token in text and "不要" not in text and "不必" not in text and "无需" not in text and "严禁" not in text:
                # 更精确：逐行
                pass
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            low = line.lower()
            if any(h in line for h in ("不要", "无需", "不必", "不得", "严禁")):
                continue
            for token in ("requireadministrator", "setuid", "se_debug_privilege"):
                assert token not in low, f"{path}:{i} 出现提权特征: {line.strip()}"

# -*- coding: utf-8 -*-
"""安全：密钥不入库、.env 不提交、日志脱敏、依赖与权限（SEC-01～SEC-07）。

本文件直接扫描仓库，不依赖开发模块（日志脱敏除外）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from conftest import require_impl

SKIP_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "node_modules",
    "tests",  # 测试夹具可能含有「假 token」字样或脱敏样例
}

# 疑似真实密钥，而不是「token」这个词本身
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk_live_[A-Za-z0-9]{16,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|secret_key|access_token|app_secret|private_key)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"
    ),
]

TEXT_SUFFIXES = {
    "",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".env",
    ".md",
    ".txt",
    ".cfg",
    ".conf",
    ".sh",
    ".xml",
    ".toml",
    ".properties",
}


def _iter_text_files(root: Path, include_tests: bool = False):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            if not include_tests:
                continue
            if "tests" in p.parts and not include_tests:
                continue
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {".env", ".gitignore"}:
            # 仍扫描无后缀的小文本
            if p.suffix:
                continue
        if p.stat().st_size > 2_000_000:
            continue
        yield p


def _scan_text(text: str):
    hits = []
    for pat in SECRET_PATTERNS:
        for m in pat.finditer(text or ""):
            hits.append((pat.pattern, m.group(0)[:40]))
    return hits


def test_sec01_workspace_has_no_plaintext_secrets(repo_root):
    """SEC-01 工作区扫描。"""
    findings = []
    for p in _iter_text_files(repo_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = _scan_text(text)
        if hits:
            findings.append((str(p.relative_to(repo_root)), hits))
    assert not findings, "工作区疑似含明文密钥:\n" + "\n".join(
        f"  {path}: {hits}" for path, hits in findings
    )


def test_sec02_git_history_has_no_secrets(repo_root):
    """SEC-02 git 历史扫描。"""
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        pytest.skip("不是 git 仓库，跳过历史扫描")
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-p", "--all", "--pretty=format:"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        pytest.skip(f"无法读取 git 历史: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"git log 失败: {proc.stderr}")
    hits = _scan_text(proc.stdout)
    assert not hits, f"git 历史中疑似出现密钥: {hits[:5]}"


def test_sec03_env_not_committed_and_gitignored(repo_root):
    """SEC-03 .env 不提交，.gitignore 包含 .env。"""
    gi = repo_root / ".gitignore"
    assert gi.exists(), "仓库根目录缺少 .gitignore"
    text = gi.read_text(encoding="utf-8", errors="ignore")
    lines = [ln.strip() for ln in text.splitlines()]
    assert any(ln in {".env", "**/.env", "/.env", ".env*"} or ln.endswith(".env") for ln in lines), (
        ".gitignore 未包含 .env"
    )
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        pytest.skip("不是 git 仓库，仅检查了 gitignore")
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=False,
    )
    tracked = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    env_tracked = [f for f in tracked if f == ".env" or f.endswith("/.env") or f.endswith(".env.local")]
    assert not env_tracked, f".env 已被 git 跟踪: {env_tracked}"


def test_sec04_log_redaction():
    """SEC-04 日志脱敏。模块未提供 redact_log 时 skip。"""
    impl = require_impl("redact_log")
    # 以下均为虚构样例，不是真实账号
    samples = {
        "user phone=13800138000 ok": "138",
        "wxid_mockexample001 joined": "wxid_",
        "access_token=abcdEFGH12345678": "token",
        "微信号: fakewx_demo_user": "微信",
    }
    phone_line = impl.redact_log("告警：手机号 13800138000 登录失败")
    assert "13800138000" not in phone_line, f"手机号未被脱敏: {phone_line!r}"
    assert re.search(r"138\*+|138\d?\*+", phone_line) or "****" in phone_line

    wxid_line = impl.redact_log("消息来自 wxid_mockexample001")
    assert "wxid_mockexample001" not in wxid_line, f"wxid 未被脱敏: {wxid_line!r}"

    token_line = impl.redact_log("refresh token=superSecretTokenValue999")
    assert "superSecretTokenValue999" not in token_line, f"token 未被脱敏: {token_line!r}"

    wx_line = impl.redact_log("微信号: fakewx_demo_user")
    assert "fakewx_demo_user" not in wx_line, f"微信号未被脱敏: {wx_line!r}"


def test_sec05_dependency_audit(repo_root):
    """SEC-05 依赖漏洞。无锁文件/产品依赖清单则记为暂不可测（skip）。"""
    product_dep_files = [
        repo_root / "requirements.txt",
        repo_root / "requirements-lock.txt",
        repo_root / "poetry.lock",
        repo_root / "Pipfile.lock",
        repo_root / "package-lock.json",
        repo_root / "pnpm-lock.yaml",
        repo_root / "yarn.lock",
        repo_root / "src" / "requirements.txt",
    ]
    present = [p for p in product_dep_files if p.exists()]
    if not present:
        pytest.skip("产品目录尚无依赖清单或锁文件，SEC-05 暂不可测（不记通过）")

    findings = []
    if any(p.name.startswith("requirements") or p.suffix == ".lock" and "package" not in p.name for p in present):
        req = next((p for p in present if "requirements" in p.name), None)
        if req:
            proc = subprocess.run(
                ["pip", "audit", "-r", str(req)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode in {127, 2} and "No such file" in (proc.stderr or ""):
                pytest.skip("pip audit 不可用")
            combined = (proc.stdout or "") + (proc.stderr or "")
            if proc.returncode != 0 and "No known vulnerabilities" not in combined:
                if (
                    "No module named" in combined
                    or "unknown command" in combined.lower()
                    or proc.returncode == 127
                ):
                    pytest.skip("pip audit 不可用，SEC-05 暂不可测（不记通过）")
                findings.append(proc.stdout or proc.stderr)
    if any(p.name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"} for p in present):
        proc = subprocess.run(["npm", "audit", "--audit-level=high"], cwd=str(repo_root),
                              capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode not in {0, 127} and "not found" not in (proc.stderr or "").lower():
            findings.append(proc.stdout or proc.stderr)
    assert not findings, "依赖存在需处理的漏洞:\n" + "\n".join(findings)


def test_sec07_sensitive_file_permissions(repo_root):
    """SEC-07 若存在密钥文件，不得 world 可读。"""
    skip_parts = {".git", "tests", ".venv", "venv", "node_modules", "site-packages"}
    suspects = list(repo_root.glob(".env")) + list(repo_root.glob("**/*.pem")) + list(repo_root.glob("**/*.key"))
    suspects = [
        p for p in suspects
        if p.is_file() and not any(part in skip_parts for part in p.parts)
    ]
    bad = []
    for p in suspects:
        mode = p.stat().st_mode & 0o077
        if mode & 0o004:
            bad.append(f"{p} mode={oct(p.stat().st_mode)}")
    assert not bad, "敏感文件对 others 可读: " + ", ".join(bad)

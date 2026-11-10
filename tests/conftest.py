# -*- coding: utf-8 -*-
"""测试公共夹具：同时服务本仓库单元测试与核查套件。"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from src.clock import FakeClock

# 保证核查用例可 `import constants` / `import adapter`
_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapter import AdapterNotReady, require_attr  # noqa: E402


def require_impl(*names: str):
    """获取开发模块；接口缺失时 skip，而不是当框架错误。"""
    try:
        return require_attr(*names)
    except AdapterNotReady as exc:
        pytest.skip(str(exc))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 11, 10, 0, 0))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def repo_root() -> Path:
    return _REPO_ROOT


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


@pytest.fixture
def messages() -> list[dict]:
    return _load_jsonl(_TESTS_DIR / "data" / "messages.jsonl")


@pytest.fixture
def negative_messages() -> list[dict]:
    return _load_jsonl(_TESTS_DIR / "data" / "negative_forbidden.jsonl")

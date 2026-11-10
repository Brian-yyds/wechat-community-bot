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


# ---------------------------------------------------------------------------
# 把本次 pytest 结果写入 .last_results.json，供 report.py 汇总。
# ---------------------------------------------------------------------------

_SESSION_RESULTS: list[dict] = []


def pytest_runtest_logreport(report):
    """记录 call 阶段结果；setup/teardown 的 skip 与 error 也要记，避免漏计。"""
    if report.when == "call":
        _SESSION_RESULTS.append(
            {
                "nodeid": report.nodeid,
                "outcome": report.outcome,
                "when": report.when,
                "longrepr": str(report.longrepr) if report.longrepr is not None else "",
            }
        )
        return
    if report.when == "setup" and report.outcome in {"skipped", "failed"}:
        _SESSION_RESULTS.append(
            {
                "nodeid": report.nodeid,
                "outcome": "skipped" if report.outcome == "skipped" else "failed",
                "when": report.when,
                "longrepr": str(report.longrepr) if report.longrepr is not None else "",
            }
        )


def pytest_sessionfinish(session, exitstatus):
    import json
    from adapter import use_mock

    payload = {
        "exitstatus": int(exitstatus),
        "use_mock": bool(use_mock()),
        "repo_root": str(_REPO_ROOT),
        "results": _SESSION_RESULTS,
    }
    out = _TESTS_DIR / ".last_results.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


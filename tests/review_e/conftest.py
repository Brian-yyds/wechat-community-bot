# -*- coding: utf-8 -*-
"""模块 E 补测夹具：隔离数据目录，禁止污染仓库 data/PAUSE 与 data/latency.jsonl。"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_PAUSE = REPO_ROOT / "data" / "PAUSE"
REAL_LATENCY = REPO_ROOT / "data" / "latency.jsonl"


def _snapshot(path: Path) -> tuple[bool, int | None, str | None]:
    if not path.exists():
        return (False, None, None)
    data = path.read_bytes()
    return (True, path.stat().st_mtime_ns, data.hex())


@pytest.fixture(autouse=True)
def _protect_real_data_files():
    """任何本目录用例都不得创建、删除或改写仓库里的 data/PAUSE、data/latency.jsonl。"""
    before_pause = _snapshot(REAL_PAUSE)
    before_lat = _snapshot(REAL_LATENCY)
    yield
    after_pause = _snapshot(REAL_PAUSE)
    after_lat = _snapshot(REAL_LATENCY)
    assert after_pause == before_pause, "补测触碰了仓库 data/PAUSE，必须改为使用临时目录"
    assert after_lat == before_lat, "补测触碰了仓库 data/latency.jsonl，必须改为使用临时目录"

# -*- coding: utf-8 -*-
"""隔离紧急暂停与数据目录，禁止触碰仓库 data/PAUSE。"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REPO_PAUSE = REPO / "data" / "PAUSE"
REPO_ALERTS = REPO / "data" / "uia_alerts.jsonl"
REPO_SENT_IDS = REPO / "data" / "uia_sent_ids.json"


@pytest.fixture(autouse=True)
def _isolate_repo_pause_and_data(tmp_path, monkeypatch):
    """所有补测把 PAUSE 与 data 指到临时目录；不得创建或删除仓库 data/PAUSE。"""
    existed_pause = REPO_PAUSE.exists()
    pause_stat = REPO_PAUSE.stat() if existed_pause else None
    existed_alerts = REPO_ALERTS.exists()
    existed_sent = REPO_SENT_IDS.exists()

    monkeypatch.setenv("WECHAT_BOT_PAUSE_FILE", str(tmp_path / "REPO_ISOLATED_PAUSE"))
    monkeypatch.setenv("WECHAT_BOT_DATA_DIR", str(tmp_path / "isolated_data"))

    yield

    if existed_pause:
        assert REPO_PAUSE.exists(), "核查补测不得删除已有的仓库 data/PAUSE"
        assert REPO_PAUSE.stat().st_mtime == pause_stat.st_mtime, "核查补测不得改写已有的仓库 data/PAUSE"
    else:
        assert not REPO_PAUSE.exists(), "核查补测不得创建仓库 data/PAUSE"
    if not existed_alerts:
        assert not REPO_ALERTS.exists(), "核查补测不得写入仓库 data/uia_alerts.jsonl"
    if not existed_sent:
        assert not REPO_SENT_IDS.exists(), "核查补测不得写入仓库 data/uia_sent_ids.json"

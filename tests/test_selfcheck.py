"""自检脚本可在仓库根目录跑通。"""

from __future__ import annotations

from pathlib import Path

from scripts import selfcheck


def test_selfcheck_passes_on_example_config(tmp_path: Path, monkeypatch):
    root = Path("/workspace/wechat-community-bot")
    monkeypatch.chdir(root)
    code = selfcheck.main(["--config", str(root / "config" / "example.toml")])
    assert code == 0

"""端到端演示：注入消息 → 分类 → 自动回复 → 定时公告 → 限速发出。"""

from __future__ import annotations

from pathlib import Path

from src.main import main, run_demo


def test_run_demo_end_to_end(tmp_path: Path):
    summary = run_demo(data_dir=tmp_path / "data", project_root=Path("/workspace/wechat-community-bot"))
    results = summary["results"]
    assert len(results) >= 6
    assert any(r["duplicate"] for r in results)
    categories = {r["category"] for r in results if r["category"]}
    assert "提问" in categories
    assert "闲聊/无关" in categories
    assert "广告刷屏" in categories
    assert "公告回复" in categories
    assert "行情讨论" in categories
    assert any(r["reply_id"] for r in results)
    assert summary["sent"]
    assert summary["fired_announcements"]
    assert Path(summary["jsonl_path"]).exists()


def test_main_demo_exit_zero(tmp_path: Path, capsys):
    code = main(["--demo", "--data-dir", str(tmp_path / "data")])
    assert code == 0
    out = capsys.readouterr().out
    assert "演示结束" in out
    assert "严禁" in out

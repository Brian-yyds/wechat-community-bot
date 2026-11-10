"""简单的 JSON 原子写入与读取，供限速窗口、运行模式等状态落盘。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, data: Any) -> None:
    """先写临时文件再替换，避免写到一半中断导致损坏。"""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, dest)


def read_json(path: str | Path, default: Any = None) -> Any:
    """读取 JSON。文件不存在或损坏时返回 default，不抛给调用方。"""
    dest = Path(path)
    if not dest.exists():
        return default
    try:
        text = dest.read_text(encoding="utf-8")
        if not text.strip():
            return default
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return default

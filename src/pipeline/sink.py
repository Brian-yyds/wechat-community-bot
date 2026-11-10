"""结构化记录落盘（JSONL）。默认写入 data/，该目录已被 gitignore。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping


class JsonlSink:
    """将处理记录以 JSONL 追加写入磁盘，线程安全。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(dict(record), ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

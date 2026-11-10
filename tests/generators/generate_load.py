#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""压测用 burst（短时高并发）与刷屏（同文重复）序列生成器。

全部为虚构消息，不含真实人名或手机号。默认写到 tests/data/generated/（不入库）。

用法：
  python3 generators/generate_load.py burst --n 200 --interval-ms 50
  python3 generators/generate_load.py spam --n 40 --text "重复刷屏示例"
  python3 generators/generate_load.py mixed --n 300
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"

FAKE_SENDERS = [f"压测用户{i:03d}" for i in range(1, 41)]
GROUPS = ["交易会员群-主群", "交易会员群-新手指引", "交易会员群-行情交流"]

BURST_TEXTS = [
    "请问开户入口在哪里？",
    "提币 pending 了怎么办？",
    "手续费是多少？",
    "BTC 今天怎么看？",
    "收到",
    "周末有空一起打球吗？",
    "加我微信 loadtest_fake_001 带单",
    "请管理员人工看一下我的权益。",
]


def _ts(start: datetime, i: int, interval_ms: int) -> str:
    t = start + timedelta(milliseconds=interval_ms * i)
    return t.isoformat()


def generate_burst(n=100, group=None, interval_ms=50, start=None, sender_cycle=True):
    start = start or datetime(2026, 11, 11, 10, 0, 0, tzinfo=SHANGHAI)
    group = group or GROUPS[0]
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"burst{i:05d}",
                "group": group,
                "sender": FAKE_SENDERS[i % len(FAKE_SENDERS)],
                "ts": _ts(start, i, interval_ms),
                "text": BURST_TEXTS[i % len(BURST_TEXTS)],
                "note": "压测 burst，无期望分类，仅用于限流/延迟灌入",
            }
        )
    return rows


def generate_spam_repeat(text="🔥 名额有限加我微信 spam_fake_001", n=30, group=None,
                         sender="刷屏号-压测", interval_ms=200, start=None):
    start = start or datetime(2026, 11, 11, 10, 5, 0, tzinfo=SHANGHAI)
    group = group or GROUPS[0]
    rows = []
    for i in range(n):
        rows.append(
            {
                "id": f"spam{i:05d}",
                "group": group,
                "sender": sender,
                "ts": _ts(start, i, interval_ms),
                "text": text,
                "expected_category": "广告刷屏",
                "note": f"压测刷屏序列 {i+1}/{n}",
            }
        )
    return rows


def generate_mixed_flood(n=300, interval_ms=80, start=None):
    start = start or datetime(2026, 11, 11, 10, 10, 0, tzinfo=SHANGHAI)
    burst = generate_burst(n=n * 2 // 3, interval_ms=interval_ms, start=start)
    spam = generate_spam_repeat(
        n=n - len(burst),
        interval_ms=interval_ms,
        start=start + timedelta(milliseconds=3),
        group=GROUPS[1],
    )
    rows = burst + spam
    rows.sort(key=lambda r: r["ts"])
    for i, r in enumerate(rows):
        r["id"] = f"mix{i:05d}"
    return rows[:n]


def _dump(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} lines -> {path}")


def main(argv=None):
    p = argparse.ArgumentParser(description="生成 burst / 刷屏压测序列")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("burst")
    b.add_argument("--n", type=int, default=200)
    b.add_argument("--interval-ms", type=int, default=50)
    b.add_argument("--group", default=GROUPS[0])
    s = sub.add_parser("spam")
    s.add_argument("--n", type=int, default=40)
    s.add_argument("--text", default="🔥 名额有限加我微信 spam_fake_001")
    s.add_argument("--interval-ms", type=int, default=200)
    m = sub.add_parser("mixed")
    m.add_argument("--n", type=int, default=300)
    m.add_argument("--interval-ms", type=int, default=80)
    p.add_argument("--out", default="")
    args = p.parse_args(argv)

    if args.cmd == "burst":
        rows = generate_burst(n=args.n, group=args.group, interval_ms=args.interval_ms)
        out = Path(args.out) if args.out else OUT_DIR / "burst.jsonl"
    elif args.cmd == "spam":
        rows = generate_spam_repeat(text=args.text, n=args.n, interval_ms=args.interval_ms)
        out = Path(args.out) if args.out else OUT_DIR / "spam.jsonl"
    else:
        rows = generate_mixed_flood(n=args.n, interval_ms=args.interval_ms)
        out = Path(args.out) if args.out else OUT_DIR / "mixed.jsonl"
    _dump(out, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

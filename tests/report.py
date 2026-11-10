#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据最近一次 pytest 结果汇总 tests/REPORT.md。"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
RESULTS = TESTS_DIR / ".last_results.json"
REPORT = TESTS_DIR / "REPORT.md"
SHANGHAI = timezone(timedelta(hours=8))

# 用例名前缀或文件 -> 验收编号，便于退回清单
FILE_TO_SECTION = {
    "test_classification.py": "识别准确率（CL）",
    "test_latency.py": "回复延迟（LT）",
    "test_scheduler.py": "定时公告（SC）",
    "test_rate_limit.py": "封号风险（RK）",
    "test_security.py": "安全（SEC）",
    "test_forbidden_scan.py": "禁止功能扫描（FB）",
}


def _git_status(repo: Path) -> str:
    if not (repo / ".git").exists():
        return "不是 git 仓库。"
    def run(args):
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
        return (p.stdout or p.stderr or "").strip()
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    commit = run(["log", "-1", "--pretty=%h %s"])
    remote = run(["remote", "-v"])
    status = run(["status", "--short", "--branch"])
    remote_txt = remote if remote else "（未配置 remote，未执行 push，也不应擅自 push）"
    return (
        f"- 分支：`{branch}`\n"
        f"- 最近提交：{commit or '无'}\n"
        f"- remote：{remote_txt}\n"
        f"- 工作区：\n```\n{status or 'clean'}\n```\n"
    )


def _case_file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].split("/")[-1]


def main():
    if not RESULTS.exists():
        REPORT.write_text(
            "# 测试报告\n\n未找到 `.last_results.json`。请先运行 `./run_all.sh` 或 `./run_all.sh mock`。\n",
            encoding="utf-8",
        )
        print(f"missing results, wrote stub {REPORT}")
        return 1

    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    results = data.get("results") or []
    # 去重后的终态
    by = {"passed": [], "failed": [], "skipped": []}
    for r in results:
        by.setdefault(r["outcome"], []).append(r)

    n_pass, n_fail, n_skip = len(by["passed"]), len(by["failed"]), len(by["skipped"])
    total = n_pass + n_fail + n_skip
    use_mock = data.get("use_mock")
    exitstatus = data.get("exitstatus")
    now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S") + "（Asia/Shanghai）"

    if n_fail:
        overall = "不通过"
    elif n_pass == 0 and n_skip:
        overall = "未测（全部跳过）"
    else:
        overall = "通过" if exitstatus == 0 else "不通过"

    repo = Path(data.get("repo_root") or TESTS_DIR.parent)

    lines = []
    lines.append("# 「交易会员群」机器人测试报告")
    lines.append("")
    lines.append(f"- 生成时间：{now}")
    lines.append(f"- 模式：{'脚手架 mock（tests/mock，非产品实现）' if use_mock else '真实模块（缺失则 skip）'}")
    lines.append(f"- 结论：**{overall}**")
    lines.append(f"- 统计：通过 {n_pass} / 不通过 {n_fail} / 跳过 {n_skip}（共 {total}）")
    lines.append("")

    # 按文件汇总
    lines.append("## 分项摘要")
    lines.append("")
    groups = defaultdict(lambda: {"passed": 0, "failed": 0, "skipped": 0, "fails": [], "skips": []})
    for r in results:
        fn = _case_file(r["nodeid"])
        g = groups[fn]
        g[r["outcome"]] = g.get(r["outcome"], 0) + 1
        if r["outcome"] == "failed":
            g["fails"].append(r)
        if r["outcome"] == "skipped":
            g["skips"].append(r)

    lines.append("| 文件 | 验收范围 | 通过 | 不通过 | 跳过 |")
    lines.append("| --- | --- | ---: | ---: | ---: |")
    for fn in sorted(groups):
        g = groups[fn]
        lines.append(
            f"| `{fn}` | {FILE_TO_SECTION.get(fn, '其他')} | {g['passed']} | {g['failed']} | {g['skipped']} |"
        )
    lines.append("")

    lines.append("## 不通过与具体问题")
    lines.append("")
    if not by["failed"]:
        lines.append("无失败用例。")
        lines.append("")
    else:
        for r in by["failed"]:
            lines.append(f"### `{r['nodeid']}`")
            detail = (r.get("longrepr") or "").strip() or "（无详情）"
            # 截断过长 traceback
            if len(detail) > 2000:
                detail = detail[:2000] + "\n…（已截断）"
            lines.append("")
            lines.append("```")
            lines.append(detail)
            lines.append("```")
            lines.append("")

    lines.append("## 跳过（模块未交付或暂不可测）")
    lines.append("")
    if not by["skipped"]:
        lines.append("无跳过用例。")
        lines.append("")
    else:
        # 压缩相同原因
        reasons = defaultdict(list)
        for r in by["skipped"]:
            reason = (r.get("longrepr") or "").strip()
            # pytest skip 格式：Skipped: reason
            if "Skipped:" in reason:
                reason = reason.split("Skipped:", 1)[-1].strip()
            reasons[reason or "未给出原因"].append(r["nodeid"])
        for reason, nodes in reasons.items():
            lines.append(f"- （{len(nodes)}）{reason}")
            if len(nodes) <= 8:
                for n in nodes:
                    lines.append(f"  - `{n}`")
            else:
                lines.append(f"  - 例如：`{nodes[0]}` 等 {len(nodes)} 条")
        lines.append("")

    lines.append("## 退回开发的修复项")
    lines.append("")
    if not by["failed"] and not use_mock:
        if by["skipped"]:
            lines.append(
                "当前没有失败项。存在跳过项，表示对应模块尚未交付或接口未对齐；"
                "请按 `tests/README.md` 实现适配层约定后复测。"
            )
        else:
            lines.append("本轮无退回项。")
    elif not by["failed"] and use_mock:
        lines.append(
            "本轮为脚手架 mock 自检，失败项为空只说明**测试用例逻辑自洽**，"
            "不代表产品已通过验收。开发交付后请用 `./run_all.sh real` 复测。"
        )
    else:
        lines.append("请按下列失败用例修复后通知核查复测：")
        for r in by["failed"]:
            fn = _case_file(r["nodeid"])
            lines.append(f"- [{FILE_TO_SECTION.get(fn, fn)}] `{r['nodeid']}`")
        lines.append("")
        lines.append("修复要求：不得为通过测试而放宽禁止功能边界；不得把负向用例做成功能。")
    lines.append("")

    lines.append("## Git 状态（未执行 push）")
    lines.append("")
    lines.append(_git_status(repo))
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append(
        "- 稳定性中的 24h/72h 长时观察（ST-05/ST-06/ST-07）默认不在本次自动化范围内，须按 `ACCEPTANCE.md` 在预发单独执行。"
    )
    lines.append(
        "- 根目录 `.gitignore` 含有 `data/`，会忽略 `tests/data/` 下的数据集。"
        "建议开发在根 `.gitignore` 增加 `!tests/data/` 与 `!tests/data/**`；当前可用 `git add -f tests/data/*.jsonl`。"
    )
    lines.append("- 禁止功能扫描若有命中，除退回修复外须立即报告智哥。")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT}  overall={overall}  pass={n_pass} fail={n_fail} skip={n_skip}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

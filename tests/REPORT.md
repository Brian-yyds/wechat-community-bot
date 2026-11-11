# 「交易会员群」机器人测试报告（总览）

- 生成时间：2026-11-11 04:30:46（Asia/Shanghai）
- **终审结论：有条件通过**（模拟与单元 0 失败；真机未验，不承诺不封号）
- 完整终审报告：**请以 [`tests/REPORT_FINAL.md`](REPORT_FINAL.md) 为准。**

本文件只作入口。分模块历史报告见 `REPORT_A.md` / `REPORT_B.md` / `REPORT_CD.md` / `REPORT_E.md`。Windows 人工实测与 7–14 天观察见 `WINDOWS_MANUAL_TEST.md`、`observation_log.csv`。

## 全量复测数字

| 模式 | 时间（Asia/Shanghai） | 通过 | 失败 | 跳过 | 合计 |
| --- | --- | ---: | ---: | ---: | ---: |
| real（`BOT_TEST_USE_MOCK=0`，pytest） | 2026-11-11 04:28:32–04:29:22 | 933 | 0 | 1 | 934 |
| mock（`./tests/run_all.sh mock`） | 2026-11-11 04:29:43–04:30:25 | 933 | 0 | 1 | 934 |

- 仍失败项：无。
- 跳过：`tests/test_security.py:206`，`pip audit` 不可用（SEC-05，不记通过）。
- 模块结论：A/B/C/D **有条件通过**，E **通过**，总体 **有条件通过**。
- 对照开发 HEAD：`4b49eab`。核查侧仅本地提交 `tests/`，未 push，仓库无 remote。

条件摘要：必须在用户 Windows 专用小号上按 `WINDOWS_MANUAL_TEST.md` 实测并观察 7–14 天；B/C/D 真机骨架与 A 的 4.x 表结构尚未在本轮验证；**不承诺不封号**。

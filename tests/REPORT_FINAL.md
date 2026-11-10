# 「交易会员群」微信机器人终审报告

- 生成时间：2026-11-11 04:30:46（Asia/Shanghai）
- 仓库：`/workspace/wechat-community-bot`
- 对照开发提交（HEAD，复测前）：`4b49eab`（`test: 补充水位线防重放与自检幂等用例`）
- 核查测试提交：见文末（仅本地 `tests/` 提交，未 push）
- 复测范围：A（本地库只读）、B（UIA 发送）、C（通知唤醒）、D（OCR 降级）、E（运行模式 + 延迟打点）
- **总结论：有条件通过**
- 模拟与单元测试：无失败项。不得据此承诺真机可用或不封号。

本报告是终审总览。分模块首轮/复测细节见 `tests/REPORT_A.md`、`REPORT_B.md`、`REPORT_CD.md`、`REPORT_E.md`。Windows 人工实测步骤与 7–14 天观察表见 `tests/WINDOWS_MANUAL_TEST.md`、`tests/observation_log.csv`。

---

## 1. 总结论

在当前工作区、对照开发已提交的 A/B/C-D/E 全部待修项（含 B `0105bab` / `f0df58f` / `e02309b`，C/D `2bfebf7` / `ad5aef8` / `249140c`，E `90e388b` / `c4864f1` / `f7b4cce` / `ec4b79f` / `abe5e04`，A `6612126` / `8b373b8` / `4b49eab`）上完成最终全量复测。

| 模块 | 终审结论 |
| --- | --- |
| A 本地库只读抓取 | **有条件通过** |
| B UIA 限频发送 | **有条件通过** |
| C 通知预览唤醒 | **有条件通过** |
| D OCR 降级发送 | **有条件通过** |
| E 运行模式与延迟打点 | **通过** |
| **总体** | **有条件通过** |

条件（必须同时满足，缺一不可）：

1. 只在用户 Windows 电脑、**可丢弃的专用小号**上按 `tests/WINDOWS_MANUAL_TEST.md` 做人工实测；本轮**没有**真实操作微信界面，也没有在 box 上登录微信。
2. 真实路径里，A 的 4.x 表结构、B 的 `PyWeixinDriver`、C 的系统通知读取、D 的 OCR 与键鼠都还是**未在真机验证的骨架**，必须先用小号跑通再扩大范围。
3. **不承诺不封号，也不承诺「一周必不封」「观察期满即可视为安全」。** 必须按清单做 **7–14 天观察**，出现风控信号立即停止并报告智哥。
4. 严禁博彩 / 六合彩 / 下注 / 结算 / 收款等功能；本轮扫描与测试均未发现实现，若以后发现立即停止并上报。

开发报告的 18 个失败用例已核对：全部是核查侧用例固化了修复前的旧行为，不是新缺陷。已更新用例并补上确认项后，全量复测 **0 失败**。

---

## 2. 测试数字

两轮均未创建或改写仓库 `data/PAUSE`、`data/latency.jsonl`（这两项在复测前后均不存在）。selfcheck 未在本轮作为验收入口执行；若需复查，只允许 dry-run / `--simulate`。

| 模式 | 命令 | 时间（Asia/Shanghai） | 通过 | 失败 | 跳过 | 合计 | 退出码 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 真实模块（real） | `.venv/bin/python -m pytest -c pytest.ini`（`BOT_TEST_USE_MOCK=0`） | 2026-11-11 04:28:32–04:29:22 | 933 | 0 | 1 | 934 | 0 |
| 脚手架 mock | `./tests/run_all.sh mock` | 2026-11-11 04:29:43–04:30:25 | 933 | 0 | 1 | 934 | 0 |

- **仍失败项：无。**
- 唯一跳过：`tests/test_security.py:206`（`test_sec05` 一类）——`pip audit` 不可用，SEC-05 暂不可测，**不记通过**。
- 1 条警告：将 `notification` 配成唯一 inbound 时，工厂按设计发出「不能作为唯一消息源」警告（`tests/test_adapter_notification.py:151`），属预期。
- mock 与 real 数字一致，是因为产品模块均已交付，契约与审查用例都打到真实 `src/`；mock 模式不能代替真机验收，也不能单独作为「产品已通过」的依据。

本轮为核对旧用例并补确认，更新/新增的核查用例包括：

- C/D：机械 OCR 用例 helper 增加 `allow_ungated_for_tests=True`；新增无闸门默认拒发、observe 下 `send_text` 被拒、挂闸门后遵守运行模式 / PAUSE / 每小时 40 条。
- A：`test_already_trimmed_ids_are_not_replayed_after_file_replace` 改为断言不重放；补水位线 + 时间戳门闩、库替换不重放、水位线之后新消息不漏。
- E：非法 `run.mode` 降级后保持已有合法状态（如 reply）；补状态文件非法/未知值回退、启动打印模式来源、调度器公告写 `latency.jsonl`、1.5–2.4 秒口径与主要文档一致。
- B：确认已有用例覆盖 NFKC 全角标题匹配、`send` 全异常→失败+PAUSE、`press_send` 后异常为 `maybe_sent` 且不自动重发，本轮全部通过。

---

## 3. 逐模块状态

### 3.1 适配器 A（本地库只读抓取）——有条件通过

已核实修复：游标损坏回退备份 / 无备份停止、只读探测、`trim_seen_window` 裁剪后保留水位线与时间戳门闩、库文件替换不重放历史、水位线之后的新消息不漏、定期对账补抓、`poll` 保持增量语义、密钥不落日志。模拟库上漏抓与重复处理为 0。未发现博彩 / 收款 / Hook / 协议实现。不内置解密或密钥提取。

残留条件：真实微信 4.x 表结构必须在用户 Windows 上验证；用户需自行提供已解密库；只读保证在模拟层已通过，真机路径与文件锁仍需人工看一眼。详见 `tests/REPORT_A.md` 与 `tests/WINDOWS_MANUAL_TEST.md` 第 4 节方案 A。

### 3.2 适配器 B（UIA 发送）——有条件通过

已核实修复：公开 `send` / `send_text`（含 Composite）在挂接 `HumanizedSender` 后强制走限速 + 运行模式 + PAUSE；失败 / `maybe_sent` 不计成功；窗口标题 NFKC 规范化后精确匹配，全角符号不再误拒；`send` 循环全异常收敛为失败并写 PAUSE；按下发送后的异常记 `maybe_sent` 且不自动重发；粘贴/发送前复查 PAUSE；空文本拒发。未发现博彩 / 收款 / Hook。

已知边界（不记缺陷、但必须遵守）：闸门在适配器 / `HumanizedSender` 层，**未下沉到 `UiDriver`**。直接调用 `paste_text` / `press_send` 仍无闸门，生产代码不得绕过适配器出口。`PyWeixinDriver` 仍为未验证骨架。

真机必测：发错群、验证码/锁屏/掉线时暂停且不自动重登、剪贴板干扰。详见 `tests/REPORT_B.md` 与 `WINDOWS_MANUAL_TEST.md` 第 4 节方案 B。

### 3.3 适配器 C（通知预览唤醒）——有条件通过

已核实修复：`wake_a_on_preview` 关闭后不会自动接到本地库读取；预览标记 `partial`，管线不对不完整预览自动回复；通知节流与按群合并生效；不能作为唯一消息源。未发现禁止功能。

残留条件：真实 Windows 通知读取仍是未验证骨架；C 不能全量抓取，必须与 A 组合。详见 `tests/REPORT_CD.md` 与 `WINDOWS_MANUAL_TEST.md` 第 4 节方案 C。

### 3.4 适配器 D（OCR 降级发送）——有条件通过

已核实修复：群名 NFKC 规范化后严格精确匹配，子串（如「主群」）与同名多处均拒发（不唯一时写 PAUSE）；正式发送闸门已挂接；未挂闸门且未注入 `SendGate` 时默认拒发；observe 下 `send_text` 被拒；挂闸门后遵守运行模式 / PAUSE / 每小时 40 条。验证码只暂停、不自动处理。未发现禁止功能。

残留条件：真实 OCR 与键鼠是未验证骨架；延迟可达十余秒，不能套用已打开会话 3 秒目标；必须先确认群名再发。详见 `tests/REPORT_CD.md` 与 `WINDOWS_MANUAL_TEST.md` 第 4 节方案 D。

### 3.5 模块 E（运行模式 + 延迟打点）——通过

已核实修复：PAUSE 读取失败默认拒发；日志先格式化再脱敏；`latency.jsonl` 补字段、正文只写长度/哈希、坏行跳过、轮转；运行模式优先级为状态文件 > 配置 > 默认 observe；非法 `mode` 不再以 `mode_force` 覆盖已有合法状态；状态文件误写成非法/未知值时回退 observe 并重写合法值；启动打印生效模式与来源；调度器公告写入 `latency.jsonl`（不含正文）；小时窗口可落盘；`open_session_rule` 默认 1.5–2.4 秒，与 `src/humanize.py`、`src/bot_api.py`、`docs/ARCHITECTURE.md`、`docs/WINDOWS.md` 口径一致。3s/8s 是分段打点目标，不是硬指标。

说明：未附带 `LatencyTrace` 的裸 `submit_announcement` 不会凭空写延迟记录，这是调用约定，调度器路径已覆盖。`docs/PROTOCOL_RISK.md` 仍主要写「默认 2–6 秒、下限 1.5 秒」，未点明打开会话短区间 1.5–2.4 秒，列为非阻断文档建议。详见 `tests/REPORT_E.md`。

---

## 4. 禁止功能、Hook、密钥与依赖

扫描范围：`src/`、`config/`、模板类文件、`requirements.txt`、工作区与 git 历史（私钥头、常见密钥赋值）。本轮为只读扫描。

| 项 | 结果 |
| --- | --- |
| 禁止功能关键词（博彩 / 六合彩 / 下注 / 结算 / 收款 / 红包转账 / 支付等） | **未发现实现**。命中均在拦截名单、分类拒绝、注释与示例配置「严禁」说明（如 `src/safety.py`、`src/pipeline/classify.py`、`src/main.py`、`config/example.toml`）。 |
| Hook / 非官方协议实现 | **未发现**。文档与 `local_db.py` / `local_capture.py` 明确不做 Hook、不注入进程、不内置密钥提取。出现的「注入」均指测试消息、时钟或闸门依赖注入。`ctypes.windll.user32` 仅出现在 `PyWeixinDriver` 的窗口操作路径，属 UIA 辅助，不是 Hook 框架。 |
| 密钥与脱敏（工作区 + git 历史） | 工作区命中均为测试夹具中的假 `wxid_` / token，用于断言脱敏。git 历史未发现私钥。未将真实密钥入库。 |
| 依赖与权限 | `requirements.txt` 仅 `pytest`、`pyyaml`。`pywinauto` / `pyautogui` / `uiautomation` 为 Windows 可选延迟导入，未在本 box 真机验证。仓库无 remote。未发现把密钥文件提交进库。 |

`pip audit` 本环境不可用，SEC-05 跳过，不记通过。

---

## 5. 残留风险与非阻断建议

### 必须在用户 Windows 专用小号上人工验证

请严格按照 `tests/WINDOWS_MANUAL_TEST.md` 执行，并填写 `tests/observation_log.csv`。重点包括：

- 第 1–2 节：环境前置、官方 PC 微信与小号登录、权限（不要关杀软躲检测）。
- 第 3 节：小号分阶段测试（先 observe，再 announce，最后限频 reply）。
- 第 4 节按方案：
  - A：免打扰 / 折叠 / 未打开群不漏抓；重启、断线后续读不丢不重；只读；密钥不进日志。
  - B：标题全半角与发错群；验证码 / 锁屏 / 掉线立即暂停且不自动重登；剪贴板恢复；限频与每小时 40 条。
  - C：通知只唤醒、不对截断预览自动回复；关闭 `wake_a_on_preview` 后不误唤。
  - D：群名精确匹配、低置信度不发、验证码只暂停。
- 第 5 节：风险信号与立即停止条件。
- 第 6–7 节：7–14 天观察记录与结束后评估。**观察期满也不能视为「已证明不会封号」。**

### 非阻断建议（不阻碍本轮有条件通过）

1. 更新 `docs/PROTOCOL_RISK.md`，把已打开会话规则回复的 1.5–2.4 秒短区间写清楚，与 `ARCHITECTURE.md` / `WINDOWS.md` 对齐。
2. `docs/PROTOCOL.md` 仍有「第一阶段尚未接入真实协议、当前是 mock」的表述，建议与现行主路线（官方 PC 微信 + 本地库只读 + UIA 发送）对齐。
3. 生产脚本严禁直接调用 `UiDriver.paste_text` / `press_send`。
4. 环境允许时补跑 `pip audit`（SEC-05）。
5. A 的 `seen_ids` 虽已裁剪并保留水位线，仍建议在真机上观察游标文件体积与对账日志。
6. D 的端到端延迟较长，文档与运行手册应避免把 OCR 路径写成 3 秒目标。

---

## 6. 明确边界

- **不承诺不封号。** 用户已接受专用小号的封号风险。验收只检查限频合规、拟人延迟、暂停与运行模式，以及是否出现风控信号。
- **必须观察 7–14 天**，记录在 `tests/observation_log.csv`，评估准则见 `tests/WINDOWS_MANUAL_TEST.md` 第 7 节。
- 延迟目标是分段打点与分位数（规则回复且会话已打开，P95 约 3 秒；其余约 8 秒），不是「必须 2 秒内发出」的硬指标。
- 本轮未 push。仓库当前无 remote。GitHub 仓库地址待用户提供后再谈同步。
- 核查过程未改 `src/`，未建轮询例程，未真实操作界面。

---

## 7. Git 状态

- 复测对照 HEAD：`4b49eabef7db5d3a169a9c2da6455eb4e8d61d0b`
- 本报告对应的测试提交：写入本文件后，仅对 `tests/` 做本地 commit，消息为 `tests: 核查验收资产与终审报告`。
- `git remote -v`：空。未 push，也无法在无 remote 时同步。

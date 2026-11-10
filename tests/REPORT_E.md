# 模块 E 核查报告（运行模式 + 延迟打点）

- 结论：**不通过**
- 时间：2026-11-11 03:56（Asia/Shanghai，UTC+8）
- 仓库：`/workspace/wechat-community-bot`
- 指定提交：`d72146a`、`efeeb52`、`a61ce17`、`99b356e`、`23fdaba`、`2951722`
- 核查基准：工作区当前源码（在上述提交之后，另有**未提交**改动：`src/run_mode.py`、`src/ratelimit/bucket.py`、`src/ratelimit/sender.py`、`src/main.py`、`src/bot_api.py`、`src/pipeline/pipeline.py`，以及新增未跟踪文件 `src/persist.py`、`src/humanize.py`）。指定提交里尚未包含 `set_mode` 持久化、限频窗口落盘、`open_session_rule` 分档；这些出现在未提交补丁中，下面会分开写。
- 约束：未改 `src/`，未 push，未建轮询，未删除 `data/PAUSE`，补测使用临时 data 目录，未污染仓库 `data/latency.jsonl`。新增用例只放 `tests/review_e/`。

## 1. 结论摘要

发送出口对 `observe` / `announce` / `reply` 的拦截、PAUSE 文件存在即停发、默认 40/小时·20/群小时·8/分钟·3/群分钟、滑动窗口跨整点、分段打点与 p50/p95 手算对照，这些主路径是成立的。工作区未提交补丁还补上了 `set_mode`、限频窗口落盘，以及已打开会话短拟人区间（1.5–2.4s，不再叠加打字）。

但仍有必须退回的问题：PAUSE 读取失败会把异常抛出发送循环（不默认拒绝）；日志脱敏会破坏 `token=%s` 模板；`time_until_allow` 不算滑动窗口且超限无日志；示例配置显式写了 `run.mode` 时，运行时切换无法在重启后保持；非法模式拒绝启动而不是降到 `observe`；`latency.jsonl` 缺运行模式和规则回复标记、会写入正文明文、坏行会把汇总打崩、没有轮转。

未发现博彩 / 六合彩 / 结算 / 收款类功能实现。

## 2. 测试结果

### 2.1 现有相关测试（工作区当前测试文件，含其他执行者未提交改动）

共 73 项：72 通过，1 跳过，0 失败。未改这些已有文件。

| 文件 | 收集 | 结果 |
| --- | --- | --- |
| `tests/test_run_modes.py` | 13 | 通过 |
| `tests/test_run_mode.py` | 5 | 通过 |
| `tests/test_latency_trace.py` | 8 | 通过 |
| `tests/test_latency.py` | 9 | 通过 |
| `tests/test_rate_limit.py` | 13 | 通过 |
| `tests/test_ratelimit.py` | 7 | 通过 |
| `tests/test_hourly_limit.py` | 2 | 通过 |
| `tests/test_security.py` | 8 | 7 通过，1 跳过（`pip audit` 不可用，SEC-05） |
| `tests/test_logging_util.py` | 4 | 通过 |
| `tests/test_forbidden_scan.py` | 3 | 通过 |
| `tests/test_selfcheck.py` | 1 | 通过 |

跑完后仓库 `data/PAUSE`、`data/latency.jsonl` 仍不存在，没有被污染。

### 2.2 补测 `tests/review_e/`

共 57 项：46 通过，11 失败。失败项均对应下文缺陷，作为退回证据。

失败用例：

- `test_pause_read_failure_must_default_to_blocked`
- `test_pause_exists_stat_denied_must_not_fail_open`
- `test_time_until_allow_should_include_sliding_windows`
- `test_over_limit_is_queued_not_sent_and_should_be_logged`
- `test_explicit_config_mode_overrides_and_clobbers_state_on_rebuild`
- `test_mode_switch_should_be_logged`
- `test_invalid_mode_should_fall_back_to_observe_not_reply`
- `test_bad_jsonl_lines_must_be_skipped`
- `test_record_fields_include_mode_and_rule_flag`
- `test_preview_must_not_keep_message_plaintext`
- `test_setup_logging_redacts_args_without_breaking_percent_format`

通过的要点：三种模式在发送出口强制（含直接调用 `HumanizedSender.submit`）；内存/`set_mode` 即时生效；配置未写 `run.mode` 时状态文件可恢复；PAUSE 存在即拦截回复和公告，删除后恢复，内容不参与判定，拟人等待期间创建也能拦住；默认限额与滑动窗口边界、跨整点正确；公告不占用自动回复窗口；拟人延迟共同作用下实际发出不超过群 3/分钟；带 `state_path` 时窗口可在重启后恢复（未提交补丁）；p50/p95 与线性插值手算一致；空文件可汇总；已打开会话短区间 1.5–2.4s 与 3s 预算相容（未提交补丁）；模块 E 源码无禁止功能实现。

## 3. 分项核查

### 3.1 运行模式

- `observe` 只收不发（自动回复与公告都丢弃，入站仍处理落盘）。
- `announce` 只发公告不回复。
- `reply` 两者都发。
- 拦截点在 `HumanizedSender._drop_if_blocked`（出队时与真正 `send_text` 前各一次），绕过管线直接 `submit` 也会被拦。
- 未注入 `send_gate` 时发送器仍放行（兼容旧单测）；`build_app` 会注入。
- 工作区已有 `SendGate.set_mode` / `get_mode` 和 `data/run_mode.json`。配置**没有**显式 `run.mode` 时，重启可恢复。
- 问题：示例配置默认写了 `[run] mode = "observe"`，`build_app` 把这当成 `config_explicit=True`，重启以配置为准并回写状态文件，运行时切到 `reply` 无法保持。
- 问题：`set_mode` 不记切换日志。
- 问题：非法模式抛 `ConfigError`，进程起不来，不会落到 `reply`，但也不会以 `observe` 继续只读运行。空值/缺省会落到 `observe`。

### 3.2 PAUSE

- 文件存在即拒绝所有发送（含公告），不解析内容；删除后恢复。
- 拟人延迟期间创建 PAUSE，发出前第二次检查会丢弃。
- 问题：`PauseGate.is_paused()` 直接 `path.exists()`，没有「读取失败则视为已暂停」。在本环境 Python 3.13 下，父目录无权限时 `Path.exists()` 会抛 `PermissionError`，`decide()` 随之抛错，发送循环中断。
- `resume()` 是 `exists()` 后再 `unlink()`，并发 resume 存在 `FileNotFoundError` 竞态（代码层面成立；补测未稳定打到）。

### 3.3 限频

- `build_rate_limiter` 默认值符合验收：全局 40/小时、单群 20/小时、全局 8/分钟、单群 3/分钟。
- 滑动窗口（不是自然小时整点清零）。跨整点：10:30 发满 40 条，11:00 仍拦截，11:30 首条滑出后才放行。
- 超限行为：不发出，留在队列里（排队/defer），不是丢弃。但 `time_until_allow` 只看令牌桶、不算窗口，超限后等待时间为 0，发送器只能按 0.05s 或再次走拟人延迟后重试；并且超限没有日志。
- 公告占用回复额度：**不占用**。公告走独立令牌桶，不计入 40/20/8/3 窗口。
- 与拟人延迟共同作用：实际发出不超过窗口上限。
- 工作区未提交补丁会把窗口写到 `data/rate_limit_window.json`，带 `state_path` 重建后可保持。令牌桶本身仍不落盘，窗口满时足以挡住瞬间再满额。指定提交里还没有这项持久化。

### 3.4 延迟打点

- 分段字段有检出 / 决策 / 限速与拟人 / 适配器发送 / 端到端（含/不含拟人）。
- 有 `message_id`、`session_open`。**没有**运行模式字段，也**没有**规则回复标记（`rule_reply` / `reply_id` 等）。
- p50/p95 使用 `(n-1)*p` 线性插值，与手算（等同 numpy `method='linear'`）一致。空文件可汇总。
- `read_all()` 遇到坏行直接 `json.loads` 抛错，不能容错。
- 只追加写入，没有轮转或大小上限。
- `preview` 写入消息正文前 80 字，只做手机号/wxid/token 等规则脱敏，普通正文会明文落盘。
- 公告默认不附带 `LatencyTrace`，不会写入 `latency.jsonl`。
- Mock 检出延迟为 0（开发已声明），补测确认。
- 目标判定：常量是已打开会话 3s、其余 8s，不是「2 秒内」或「一周必不封」。评估口径是 `e2e_excluding_humanize_s`，拟人单独列出。
- **取舍**：默认拟人 2–6s 本身已经超过「已打开会话 3s」，不能把这一档套到 3s 目标上。工作区未提交的 `src/humanize.py` 为此增加了 `open_session_rule`（1.5–2.4s），打开会话不再叠加「正在输入」等待，使「拟人上限 + 0.5s 检出/决策预算 ≤ 3s」在纸面上成立。指定提交里还没有这档。`e2e_excluding_humanize_s` 目前只减 `humanize_delay_s`，不减 `typing_delay_s`。

### 3.5 安全与脱敏

- 日志参数中的 wxid / 手机号 / `token=...` 在过滤器正常工作时可以被脱敏；发送丢弃日志不写正文。
- 问题：`RedactFilter` 先对 **msg 模板** 做 `redact()`，`logger.info("user=%s token=%s phone=%s", ...)` 会把模板里的 `token=%s` 变成 `token=***`，随后 `getMessage()` 触发 `TypeError: not all arguments converted during string formatting`，这条日志记不下来。
- 禁止功能关键词扫描：模块 E 相关源码没有下注/开奖/代收等功能实现，只有拦截与「严禁」说明。

## 4. 具体问题

| 级别 | 位置 | 现象 |
| --- | --- | --- |
| 高 | `src/adapters/pause.py:63-65`，`src/run_mode.py:193-205` | PAUSE 读取失败（`OSError` / `PermissionError`）时 `is_paused()`/`decide()` 直接抛错，发送循环可能中断，没有「默认拒绝发送」的安全分支。 |
| 高 | `src/logging_util.py:16-18,66,95-106` | `RedactFilter` 对日志 **格式模板** 做赋值脱敏，破坏 `%s` 占位符，导致 `TypeError`，敏感日志可能丢失。 |
| 中 | `src/ratelimit/bucket.py:252-260`，`src/ratelimit/sender.py:248-327` | `time_until_allow` 不计滑动窗口，超限等待为 0；超限排队但无日志。 |
| 中 | `src/main.py:152-170`，`src/run_mode.py:138-140,179-182` | 配置显式包含 `run.mode`（示例配置默认如此）时，`set_mode` 写入的 `data/run_mode.json` 会在重启时被覆盖，切换不能保持；`set_mode` 也没有切换日志。 |
| 中 | `src/run_mode.py:64-69`，`src/main.py:156-159` | 非法 `run.mode` 抛 `ConfigError` 导致无法启动，没有降级到 `observe` 继续只读。 |
| 中 | `src/latency.py:102-181` | 落盘记录缺少运行模式、规则回复标记。 |
| 中 | `src/pipeline/pipeline.py:118-124`，`src/latency.py:166` | `latency.jsonl` 的 `preview` 保存正文明文（仅部分 PII 脱敏）。 |
| 中 | `src/latency.py:213-221` | 坏行导致 `read_all()`/`summarize()` 抛 `JSONDecodeError`。 |
| 中 | `src/latency.py:204-211` | 只追加，无轮转或大小上限，长时间运行文件会一直涨。 |
| 低 | `src/scheduler/scheduler.py:288` 附近 | 定时公告提交时不传 `trace`，默认没有延迟打点。 |
| 低 | `src/latency.py:141-143` | 「不含拟人」只减 `humanize_delay_s`，不减 `typing_delay_s`。 |
| 低 | `src/adapters/pause.py:92-96` | `resume()` 的 exists+unlink 非原子，并发可能 `FileNotFoundError`。 |
| 低 | `src/run_mode.py:187-200` | `allow_auto_reply` / `allow_announcement` 重复定义。 |

## 5. 退回修复项

请在当前未提交补丁基础上继续修，并一并提交 `src/persist.py`、`src/humanize.py` 及相关改动，不要只停在工作区。

1. **PAUSE 默认安全**：`is_paused()` / `decide()` 在 stat/读取失败时视为已暂停并拒绝发送，不要把异常抛出发送循环。`resume()` 使用原子删除或忽略已不存在的文件。
2. **修复 `RedactFilter`**：只脱敏已格式化后的日志文本，或先格式化再脱敏，禁止改写仍含 `%s` 的模板。
3. **限频等待与日志**：`time_until_allow` 必须计入分钟/小时滑动窗口；超限继续排队，并写脱敏日志（含群、窗口类型、需等待的秒数）。
4. **模式切换保持且记日志**：运行时 `set_mode` 应即时生效、写盘、打切换日志；重启后保持，不能被示例里那一行显式 `run.mode` 默默打回并覆盖状态文件。非法/损坏配置应告警并落到 `observe`，而不是拒绝启动，更不能落到 `reply`。
5. **延迟打点完整且可运维**：记录补齐运行模式、规则回复标记；禁止把正文明文写入 `latency.jsonl`；`read_all`/`summarize` 跳过坏行；增加体积或条数轮转。公告若要纳入延迟评估，调度器提交时应带上 trace。
6. **把未提交的拟人分档和限频落盘提交进库**，并在文档里写清取舍：3s/8s 评估不含拟人；已打开会话走 1.5–2.4s 短档且不再叠加打字；其余仍用 2–6s，不得套到 3s 目标上。

## 6. 必须在用户 Windows 真机验证的项

下列在 box / Mock 上无法代替，须用官方 PC 微信 + 可丢的专用小号，在用户 Windows 上做：

1. 适配器 A 真实检出延迟（Mock 为 0）：免打扰、折叠、未打开群是否仍能增量抓到，`session_open` 是否与真实会话状态一致。
2. 已打开会话的规则回复：用户可感知端到端（含短拟人 1.5–2.4s）是否落在 3s 目标附近；分段打点（检出/决策/UIA 发送）的 p50/p95。
3. 未打开会话 / 需要切群或预热：是否按 8s 目标评估；UIA 打开会话本身的耗时。
4. OCR 降级发送（十余秒）明确不能按 8s 达标，只作降级，需在真机确认不会误用为主路径。
5. 紧急暂停：验证码、异常弹窗、锁屏时是否创建 `data/PAUSE` 并立刻停止一切点击/发送；删除后是否恢复。
6. 限频在真实发送路径上是否生效（40/小时、20/群小时、8/分钟、3/群分钟），重启进程后窗口文件是否仍约束；观察 7–14 天封号与风控，**不得**把「一周必不封」写成硬指标。
7. 运行模式：改配置或 `set_mode` 后是否即时改变发送行为，微信或机器人重启后模式是否保持。
8. 长时间运行：`latency.jsonl` 增长、日志脱敏（微信号/手机号/token）、无正文落盘。
9. 断线、微信重启后续读：不丢不重（与适配器 A 对账一起看）。

## 7. 已知声明对照

| 开发声明 | 核查结果 |
| --- | --- |
| `run.mode` 在发送出口强制 | 成立（`HumanizedSender` 出口）。 |
| `data/PAUSE` 紧急暂停 | 文件存在时成立；读取失败不默认安全。 |
| 默认 40/20/8/3 | 成立。 |
| `latency.jsonl` 分段打点与 p50/p95 | 分段与分位数计算成立；字段不完整，坏行不容错，无轮转。 |
| `humanize_delay_range("open_session_rule")` 尚在补 | 指定提交中确实没有；工作区未提交补丁已实现 1.5–2.4s，需提交后再复测确认。 |
| Mock 检出延迟为 0 | 确认。 |


---

# 复测（模块 E 修复后）

- 结论：**通过**
- 时间：2026-11-11 04:14（Asia/Shanghai，UTC+8）
- 仓库：`/workspace/wechat-community-bot`
- 指定提交范围：`d3c9e0b..ecd58a2`（含 `73b51b7`、`d78b6c2`、`70f9a26`/`0c142d6`、`ed3b44b`）；核查时 HEAD 为 `f570b08`（其后还有适配器 A 的提交，未改本次结论）
- 约束：未改 `src/`，未 push，未 git commit，未建轮询，未删除 `data/PAUSE`，未污染仓库 `data/latency.jsonl`（复测前后均不存在）。新增用例只放 `tests/review_e/` 新文件：`test_retest_pause.py`、`test_retest_logging.py`、`test_retest_rate.py`、`test_retest_run_mode.py`、`test_retest_latency.py`、`test_retest_legacy.py`。按要求未再跑禁止功能关键词扫描与脱敏扫描。

## 1. 结论摘要

首轮退回的 6 组问题及 real 模式遗留 4 项均已修好，复核通过。发送出口对运行模式与 PAUSE 的强制、默认 40/20/8/3、滑动窗口跨整点、分段打点与 p50/p95，仍成立。

模块 E 本身没有新的必须退回项。全量 pytest 另有 3 个失败，均在适配器 B / C-D 的核查用例里，不属于 E，也不再是「旧缺陷证据」那条 `review_b` 绕过用例。

## 2. 测试结果

### 2.1 新增复测 `tests/review_e/test_retest_*.py`

共 28 项，全部通过。整个 `tests/review_e/`（含首轮 57 项）共 85 项，全部通过。首轮失败的 11 项现已通过。

复测前后仓库 `data/PAUSE`、`data/latency.jsonl` 均不存在，没有被污染。

### 2.2 全量 pytest（real，`BOT_TEST_USE_MOCK` 未设置）

收集 906 项：902 通过，3 失败，1 跳过（`tests/test_security.py::test_sec05_dependency_audit`，`pip audit` 不可用，与首轮相同）。

已知可能失败的 `tests/review_b` 旧缺陷证据用例 `test_direct_adapter_send_bypasses_rate_limiter_and_run_mode`：**已通过**（不再失败；直接 send 在 observe 下被拒，符合闸门生效后应更新该用例的说明）。它不是唯一失败，也已不是失败项。

其余 3 个失败为回归，均不在模块 E 范围内：

| 用例 | 现象 |
| --- | --- |
| `tests/review_b/test_b_retest_gates_and_driver_boundary.py::test_window_title_spaces_ok_fullwidth_should_normalize` | 窗口标题仅全角破折号「－」与半角「-」之差时，规范化精确匹配失败，报 `target_not_found`。 |
| `tests/review_b/test_b_retest_gates_and_driver_boundary.py::test_non_uia_error_during_paste_is_contained_and_pauses` | 粘贴阶段的非 `UiaError`（`RuntimeError`）未收敛，直接抛出发送循环。 |
| `tests/review_cd/test_retest_fixes.py::test_direct_send_text_must_not_bypass_run_mode` | `OcrOutboundAdapter` 未挂接正式发送闸门时，直接 `send_text` 仍可在 observe 下发出。 |

这 3 项应分别由适配器 B、C/D 的复测执行者跟进，不作为模块 E 不通过依据。

## 3. 首轮退回项逐条

### 3.1 PAUSE 读取失败默认拒发；resume 无竞态 — **通过**

- 证据：`src/adapters/pause.py:63-68`，`is_paused()` 在 `OSError` 时返回 `True`；`src/run_mode.py:201-209`，`SendGate.is_paused()` 再兜一层，读取失败记告警并视为已暂停，不把异常抛出发送循环。
- `resume()`：`src/adapters/pause.py:95-101` 改为直接 `unlink()`，仅捕获 `FileNotFoundError` 返回 `False`，不再先 `exists` 再删。
- 复测：`test_retest_pause_read_failure_defaults_to_blocked_and_does_not_raise`、`test_retest_pause_stat_denied_defaults_to_blocked`、`test_retest_sendgate_is_paused_swallows_oserror_and_denies`、`test_retest_concurrent_resume_has_no_exists_unlink_race`（50 轮 × 24 线程同时 resume）均通过。

### 3.2 RedactFilter 不改写 `%s` 模板 — **通过**

- 证据：`src/logging_util.py:99-117`，先 `record.getMessage()` 格式化，再对已格式化文本 `redact()`，并清空 `args`。
- 复测：`token=%s` 不再触发 `TypeError`；`wxid` / token / 手机号仍被脱敏。`test_retest_redact_filter_does_not_rewrite_percent_templates`、`test_retest_setup_logging_redacts_without_typeerror` 通过。首轮失败的 `test_setup_logging_redacts_args_without_breaking_percent_format` 现已通过。

### 3.3 time_until_allow 计入滑动窗口；超限排队并记脱敏日志 — **通过**

- 证据：`src/ratelimit/bucket.py:252-284`，`time_until_allow` 取令牌桶与分钟/小时滑动窗口等待的最大值。
- 超限：`src/ratelimit/sender.py:280-295`，等待超过 `max_rate_wait_seconds` 时记日志（含需等待秒数、kind、group）并 `deferred` 放回队列，不丢弃。日志走脱敏过滤器。
- 复测：单群每分钟满额等待为正且 ≤60s；全局每小时满额等待为正且 ≤3600s；5 条里只发出 3 条，其余排队，日志含等待时长且 `wxid_*` 群 id 被脱敏；窗口滑出后可继续发出。

### 3.4 运行模式切换、优先级与非法配置 — **通过**（有设计风险，见评价）

- `set_mode` 记切换日志：`src/run_mode.py:184-190`（「运行模式已切换：旧 → 新」），并写入 store / `data/run_mode.json`。重启后保持。
- 优先级实现：`src/run_mode.py:17-19,114-148`，`src/main.py:154-178`。有效状态文件 > 配置 `run.mode` > 默认 observe。仅 `--mode`（`build_app(..., force_mode=...)`）或 `run.mode_force=true` 时 `config_explicit=True`，用传入值覆盖并回写状态。
- 示例配置：`config/example.toml:115-124` 的 `[run]` 下 `mode` / `mode_force` 均为注释，不会覆盖状态。复测 `test_retest_example_config_does_not_clobber_state` 通过。
- 非法模式：`src/main.py:163-167` 告警后落到 observe 并继续启动，不会拒绝启动，也不会落到 reply。`test_retest_invalid_mode_falls_back_to_observe_and_starts` 通过。
- `--mode` / `mode_force` 显式覆盖：`test_retest_mode_force_and_cli_mode_override_state` 通过；`parse_args(["--demo", "--mode", "announce"])` 能解析到 `announce`。

**对优先级设计的评价：** 总体合理。状态优先才能满足「运行时 `set_mode` 重启后保持」，也不再被示例里那一行 `run.mode` 默默打回；`mode_force` 默认关闭是安全默认，不会在只读配置下悄悄开始自动回复。要强制指定必须显式加 `--mode` 或写 `mode_force=true`，符合「默认观察、显式才发送」的方向。

仍需注意的风险：

1. `data/run_mode.json` 若被误写成 `reply`（误操作、损坏后被改、权限过宽），重启后会优于配置生效，可能在操作者以为仍是 observe 时开始自动回复。缓解：启动时在日志里打印当前模式来源；需要只读时用 `--mode observe` 或 `mode_force=true`。
2. 非法 `run.mode` 时 `src/main.py:167` 会把 `mode_force=True` 再落到 observe，从而**覆盖已有状态文件**（`test_retest_invalid_mode_force_overwrites_existing_state` 固定了这一现状）。这是默认安全（不会误发），但会丢掉已经切换的 `reply`。更稳妥的做法是：非法配置忽略该项、保留有效状态，仅在没有有效状态时落到 observe，且不要因此回写状态。
3. 本地配置里长期打开 `mode_force=true` 且 `mode=reply`，每次启动都会覆盖运行时切换，需避免把它当成日常默认。

### 3.5 latency.jsonl 字段、脱敏、坏行、轮转、分位数、3s/8s — **通过**

- 字段：记录含 `run_mode`、`rule_reply` / `reply_id`、`chat_was_open`、`session_open`，以及检出 / 决策 / 适配器发送 / 端到端分段（`src/latency.py:121-124,171-193`）。管线对规则回复打标（`src/pipeline/pipeline.py:197-199`），发送口补写运行模式（`src/ratelimit/sender.py:522-532`）。Mock 适配器通常没有 `last_result.chat_was_open`，该字段存在但值可能为空，属已知限制。
- 正文：`preview_fingerprint`（`src/latency.py:75-79,185`）只写 `chars` 与 `sha256`，正文明文不会进入 jsonl。
- 坏行：`read_all`（`src/latency.py:295-310`）遇到 `JSONDecodeError` 跳过，`summarize` 可继续。
- 轮转：`size_limit` 默认 5MiB，跨天或超限时改名为 `latency.jsonl.<时间戳>`，并按 `retain_days`（默认 14）清理（`src/latency.py:46-48,257-283`）。`test_retest_jsonl_rotates_by_size` 通过。
- p50/p95：仍是 `(n-1)*p` 线性插值，与手算一致（10 个样本的 e2e p95=9.55）。
- 3s/8s 判定：常量仍是已打开会话 3s、其余 8s，不是「2 秒内」。评估口径为 `e2e_excluding_humanize_s`（拟人单独列出）。规则回复且会话已打开走 3s，其余走 8s。打开会话短拟人 1.5–2.4s 加 0.5s 预算不超过 3s；默认 2–6s 只对准 8s，不得套到 3s。`summarize()` 按 `session_open` 拆组并写入 targets 说明；未单独再按 `rule_reply` 分组，判定时应同时看这两个标记。

非阻断的残留：

- 定时公告默认仍不附带 `LatencyTrace`，不会写入 `latency.jsonl`（首轮低优先级，原现状用例仍通过）。
- `e2e_excluding_humanize_s` 仍只减 `humanize_delay_s`，不减 `typing_delay_s`。打开会话短档已不再叠加打字；其余档的打字时间仍另计。
- 首轮 `test_no_rotation_or_size_cap_api` 仍按旧 API 名（`rotate` / `max_bytes`）断言「无轮转」，与现实现（`size_limit` + `_rollover_if_needed`）对不上，该证据用例已过时。按要求未改已有文件。

### 3.6 首轮 real 模式遗留 4 项 — **全部通过**

| 项 | 状态 | 证据 |
| --- | --- | --- |
| `humanize_delay_range('open_session_rule')` | 通过 | 默认 1.5–2.4s（`src/humanize.py:45,83`，下限可配）。短上限 2.4s + 0.5s 预算 ≤ 3s。 |
| `humanize_delay(text, rng=, path=)` | 通过 | `src/bot_api.py:78-89`，`src/humanize.py:168-195`。相同种子可复现，不真实睡眠（对应 LT-08/09）。 |
| RateLimiter 小时窗口落盘重启保持（RL-05） | 通过 | `state_path` 与 `store=` 两条路径重建后，同一小时窗口内不再放行。 |
| 运行模式重启恢复（RM-04） | 通过 | `SendGate(store=..., state_path=...)` 与 `build_app` 重启后保持 `set_mode` 的结果。 |

关于 LT-02/04/05 是否硬写 1.0：这三个用例从 `humanize_delay_range(...)` 读取实现区间，并用可配的 `HUMANIZE_MIN_FLOOR`（默认 0.8s）以及 3s 预算做断言，**没有**把实现短区间硬写成 `1.0` 或 `(1.0, 2.2)`，因此开发取 1.5–2.4s 不会导致它们失败。但 `tests/constants.py` 的 `HUMANIZE_OPEN_SESSION_DEFAULT` 默认仍是 `(1.0, 2.2)`，`tests/test_latency.py` 文档也写「建议 1.0–2.2s」，与开发口径不一致，属测试文档/常量问题。按要求已在 `tests/review_e/test_retest_legacy.py` 记录，未改已有文件。

## 4. 必须在用户 Windows 真机验证的项

下列在 box / Mock 上无法代替，须用官方 PC 微信 + 可丢的专用小号，在用户 Windows 上做（与首轮第 6 节相同，状态已随修复更新）：

1. 适配器 A 真实检出延迟（Mock 仍为 0）：免打扰、折叠、未打开群是否仍能增量抓到；`session_open` / `chat_was_open` 是否与真实会话一致。
2. 已打开会话的规则回复：用户可感知端到端（含短拟人 1.5–2.4s）是否落在 3s 目标附近；分段打点（检出/决策/UIA 发送）的 p50/p95。
3. 未打开会话 / 需要切群或预热：是否按 8s 目标评估；UIA 打开会话本身的耗时。
4. OCR 降级发送（十余秒）不能按 8s 达标，只作降级，需确认不会误用为主路径。
5. 紧急暂停：验证码、异常弹窗、锁屏时是否创建 `data/PAUSE` 并立刻停止点击/发送；删除后是否恢复。读取失败默认拒发只在单元测试中验证过。
6. 限频在真实发送路径上是否生效（40/小时、20/群小时、8/分钟、3/群分钟），进程重启后窗口文件是否仍约束；观察 7–14 天封号与风控，不得把「一周必不封」写成硬指标。
7. 运行模式：`set_mode` / `--mode` / `mode_force` 后是否即时改变发送行为，微信或机器人重启后模式是否按「状态优先」保持；并确认当前模式被启动日志明确打印，以免状态文件被误写成 reply 而不自知。
8. 长时间运行：`latency.jsonl` 轮转与归档清理、日志脱敏、无正文明文落盘。
9. 断线、微信重启后续读：不丢不重（与适配器 A 对账一起看）。

## 5. 已知声明对照（复测后）

| 开发声明 | 复测结果 |
| --- | --- |
| PAUSE 读取失败默认拒发 | 成立。 |
| resume 避免 exists+unlink 竞态 | 成立。 |
| 先格式化再脱敏 | 成立，`token=%s` 不再 TypeError。 |
| latency 补字段、只写长度/sha256、坏行跳过、轮转 | 成立。 |
| 运行模式优先级：状态文件 > 配置 > 默认 observe；`--mode` 与 `run.mode_force` 显式覆盖 | 成立。非法配置回退 observe 时会以 `mode_force=True` 覆盖已有状态，见上文风险 2。 |
| open_session_rule 1.5–2.4s、humanize_delay 可注入种子、小时窗口落盘、set_mode 持久化 | 成立。 |

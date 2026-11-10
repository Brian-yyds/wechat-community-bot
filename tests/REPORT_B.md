# 适配器 B（UIA 发送）核查报告

- 结论：**不通过**
- 核查时间：2026-11-11 03:52（Asia/Shanghai，CST，UTC+8）
- 仓库：`/workspace/wechat-community-bot`
- 提交：`6e542e4`、`0b7bca5`、`f395e0c`、`1dc034e`、`0ce8ece`
- 范围：只读核查 + 补测（`tests/review_b/`）。未改 `src/`，未改已有测试文件，未 push，未跑真实界面，自检仅 dry-run。仓库 `data/PAUSE` 在核查前后均不存在，补测未创建也未删除该文件。

开发声明属实：`PyWeixinDriver` 为未在真实微信客户端上验收的骨架。未发现博彩 / 六合彩 / 结算 / 收款类功能实现，也未发现 Hook / 非官方协议代码。

正式路径（`build_app` → `HumanizedSender` + `SendGate` + `RateLimiter`）的限速与 observe/announce/reply 闸门是有的；但适配器 B 仍可被直接调用而绕过，且生产出口在失败处理、幂等、打点字段和发错群防护上未达到验收要求。

## 1. 测试结果

| 套件 | 结果 |
| --- | --- |
| `tests/test_adapter_uia.py` | 18 passed |
| 契约 AD-02（`tests/test_adapter_contracts.py -k ad02`） | 3 passed（14 deselected） |
| `scripts/selfcheck_uia.py`（无额外参数，dry-run） | 退出码 0，通过。Linux 上只用 FakeUiDriver；未指定 `--send-test`，无真实发送 |
| `tests/review_b/` 补测 | 39 collected：**14 failed，25 passed**（失败即退回项证据，日志见 `tests/review_b/logs/`） |

开发自带用例覆盖了成功发送、失败分类、幂等跳过、开头检查 PAUSE、锁屏/掉线/弹窗暂停且不自动重登、`UiaSender()` 隔离临时目录、以及经 `HumanizedSender` 的正向路径。这些用例本身为绿，但不能覆盖下面列出的缺口。

## 2. 对照文档与契约

`docs/ADAPTER_B.md` 与实现大体一致的部分：FakeUiDriver 模拟、紧急暂停约定、分段耗时结构、预热/保持会话、掉线不自动重登、同一 `send_id` 不重发、自检默认 dry-run。契约 AD-02（成功、timeout / target_not_found / ui_not_ready 可区分、超时不长时间阻塞）已通过。

与文档或验收不一致的要点：

- 文档写「本适配器**没有**绕过限速层的入口」，实现并非如此（见问题 1）。
- 文档写按下发送但校验不确定「会记为 `maybe_sent`」，首次返回状态实际是 `rejected`（见问题 6）。
- 验收要求发送必须经 `RateLimiter` 与运行模式出口；延迟分段应写入 `data/latency.jsonl` 且字段完整。当前 UIA 子分段只留在 `UiaSendResult.timings`，正式出口的 JSONL 没有带上。

## 3. 已确认可用的行为

- 存在 `data/PAUSE`（或配置/环境变量指向的暂停文件）时，`send()` 开头会拒绝，不执行打开会话 / 粘贴 / 发送。
- 锁屏、窗口不可见、掉线、`has_blocking_dialog` 为真时，会立即 `_trigger_pause` 并写入暂停文件与 `uia_alerts.jsonl`，不会调用 `attempt_login`；`PyWeixinDriver` 也没有自动重登或打码方法。
- 同一 `send_id` 在成功或 `maybe_sent` 之后不会再按发送；记录在 `uia_sent_ids.json`，新实例重启后仍可跳过（文件完好时）。
- 按下发送后校验失败 / 超时不会自动重试。
- `UiaSender()` 无参构造把暂停文件放到临时目录，不会写入仓库 `data/PAUSE`。开发用例与本次补测均用 `tmp_path` / 环境变量隔离，未见误删或误建真实 `data/PAUSE`。
- `UiaSendResult.timings` 字段齐全：`open_chat` / `paste` / `send` / `verify` / `input_pause` / `group_switch_wait` / `preheat` / `adapter_total` / `chat_was_open`。
- 适配器层群名映射是精确字典查找；未知群在 FakeUiDriver 限制 `known_chats` 时返回 `target_not_found` 且不发送。
- 换行、表情、`@` 文本会原样交给 `paste_text`。
- B 相关源码无硬编码密钥、无手机号/wxid 字面量、无禁止功能标识符，禁止关键词只出现在「严禁」语境。

## 4. 具体问题

严重级别：高 = 可能误发、绕过限频/模式、发错群或把失败当成成功；中 = 行为与约定不符或有数据/干扰风险；低 = 文档、死代码或仅需真机观察。

### 高

1. **直接调用可绕过限速层与运行模式**  
   - 位置：`src/adapters/uia.py:345-366`（`send_text` / `send`）、`src/adapters/composite.py:91-92`、`src/adapters/uia.py:815-816`（`UiaSender.send`）、`scripts/selfcheck_uia.py:229-235`（`--send-test` 直接 `adapter.send`）、以及任意 `UiDriver.paste_text` / `press_send`。  
   - 现象：`HumanizedSender` + `SendGate(observe)` 会丢弃待发项；但同一适配器上直接 `send()` / `send_text()` / `CompositeAdapter.send_text()` 仍能成功发出。适配器对象不持有 `RateLimiter` 或 `RunMode`。文档「没有绕过入口」不成立。  
   - 证据：`tests/review_b/test_b_bypass_and_gates.py` 中 `test_direct_adapter_send_bypasses_rate_limiter_and_run_mode`、`test_driver_paste_and_press_bypass_all_gates`（通过，作为绕过路径记录）。

2. **生产出口忽略 UIA 失败，仍记为发送成功**  
   - 位置：`src/ratelimit/sender.py:306-315`。`send_text` 返回 `None`，只把结果放在 `last_result`；`HumanizedSender` 不读取 `ok` / `status`，一律 `sent_count += 1` 且 `outcome="sent"`。  
   - 现象：校验失败、`maybe_sent`、暂停、找不到群时，令牌已消耗，延迟日志却写成成功。  
   - 证据：`test_humanized_sender_treats_failed_uia_send_as_success` 失败（`sent=1`）。

3. **正式出口不传 `send_id`，UIA 幂等未接入生产路径**  
   - 位置：`src/ratelimit/sender.py:306`；`CompositeAdapter.send_text` / `OutboundAdapter.send_text` 签名也没有 `send_id`。  
   - 现象：适配器层幂等只在调用方显式传 `send_id` 时生效。经 `HumanizedSender` 发出的消息重启、重放或「结果不明」后可能重发。  
   - 证据：`test_humanized_sender_does_not_pass_send_id` 失败（相同文案被按下 2 次）。

4. **粘贴/点击发送前不复查 PAUSE**  
   - 位置：`src/adapters/uia.py:410-412` 只在 `send()` 开头检查；`459` 行 `paste_text`、`465` 行 `press_send` 之前不再检查。与 `src/adapters/pause.py:8-10` 约定不符。  
   - 现象：打开会话或聚焦输入框期间若出现紧急暂停，仍会粘贴并按下发送。  
   - 证据：`test_pause_flag_is_not_rechecked_before_paste_or_press` 失败（结果仍 `ok=True`）。

5. **可能发错群：内存中的当前会话 + 搜索不核对标题**  
   - 位置：`src/adapters/pyweixin_driver.py:364-365`（`current_chat` 只返回 `_current`，不读窗口）；`436-449`（`_pywinauto_open_chat` 用 Ctrl+F 输入群名后回车，立刻把 `_current` 写成目标名，不校验标题）；`src/adapters/uia.py:437-447`（若 `current_chat() == group_name` 则跳过 `open_chat`，直接往当前窗口粘贴）。  
   - 现象：用户中途切群、搜索命中同名前缀或重名会话、搜索失败时，消息可能发进错误群，适配器仍以为目标正确。  
   - 证据：`test_stale_current_chat_must_not_send_to_wrong_group` 失败（发进「二群」）；`test_pyweixin_open_chat_does_not_verify_actual_title` 失败。

6. **真实驱动对「是否发出」和「是否掉线/验证码」过弱**  
   - 位置：`pyweixin_driver.py:282-300`（没有校验接口时 `verify_sent` 直接返回 `False`，所有真实发送都可能被标成未确认）；`302-314`（没有 `is_logged_in` 时退回 `is_window_visible()`，扫码/被踢登录窗仍可能被当成已登录）；`351-362`（没有 `has_captcha` 之类方法时 `has_blocking_dialog` 恒为 `False`）；`200-202`（pyweixin 在仅成功导入时 `find_chat_window` 可能直接返回 True）。  
   - 现象：验证码、被踢、需扫码登录可能不会进入紧急暂停；同时几乎每次真实发送都会走「按下但校验失败」分支。再叠加问题 2，上层仍可能记成功。

7. **安全探活抛出非 `UiaError` 时，`send()` 直接异常外溢**  
   - 位置：`src/adapters/uia.py:643-655` 只捕获 `UiaError`。  
   - 现象：既不写 PAUSE，也不返回失败结果，调用方若未设防会中断发送循环。  
   - 证据：`test_unexpected_safety_error_must_not_crash_or_send` 失败（`RuntimeError` 冒泡）。

### 中

8. **校验不确定时首次状态为 `rejected`，不是 `maybe_sent`**  
   - 位置：`src/adapters/uia.py:488-496`。仓库里已标记 `maybe_sent`，但返回 `UiaSendStatus.REJECTED`。超时后按下则为 `maybe_sent`，同一语义两种状态。  
   - 证据：`test_maybe_sent_does_not_resend_but_first_status_should_be_maybe_sent` 失败。二次调用会跳过，不重发这一点是对的。

9. **幂等文件损坏后以空记录继续，可能重发**  
   - 位置：`src/adapters/uia.py:213-217`。  
   - 证据：`test_corrupt_sent_id_store_must_not_silently_allow_resend` 失败（损坏后同一 `send_id` 再次成功发出）。

10. **空文本 / 纯空白仍会按发送**  
    - 位置：`src/adapters/uia.py:359-366`，发送前没有校验文本。  
    - 证据：`test_empty_and_whitespace_text_must_be_rejected` 失败。

11. **剪贴板降级路径不恢复、发送后不清空**  
    - 位置：`src/adapters/pyweixin_driver.py:451-475`。`OpenClipboard` → `EmptyClipboard` → `SetClipboardText` → Ctrl+V，不保存也不恢复用户原剪贴板，正文会留在剪贴板里，干扰用户复制粘贴。  
    - 证据：`test_pyweixin_clipboard_fallback_does_not_restore_or_clear` 失败。

12. **搜索群名使用 `type_keys(group_name)` 且未转义**  
    - 位置：`src/adapters/pyweixin_driver.py:442-444`。pywinauto 会把 `+`、`^`、`%`、`{`、`}` 当成特殊键。  
    - 证据：`test_pyweixin_type_keys_group_name_is_unsafe_for_special_chars` 失败。

13. **告警落盘与部分日志未脱敏**  
    - 位置：`src/adapters/uia.py:665-677`（`uia_alerts.jsonl` 写入原始 `message`）；日志行本身有 `redact`（证据中日志为 `138****8000 token=*** wxid_***`），但 JSONL 仍是明文。`pyweixin_driver.py:198`、`296` 的 `logger.warning(..., exc)` 未调用 `redact`，且该 logger 未挂 `RedactFilter`（`setup_logging` 只配置 `wechat_bot`）。  
    - 证据：`test_uia_alerts_and_logs_redact_phone_and_token`、`test_pyweixin_logger_calls_are_not_all_redacted` 失败。

14. **正式出口写入的 `latency.jsonl` 缺少 UIA 分段字段**  
    - 位置：`src/ratelimit/sender.py:299-315`、`src/latency.py`。只有笼统的 `adapter_send_s`，`session_open` 为 `None`，没有 `open_chat` / `paste` / `send` / `verify` / `chat_was_open`。适配器自身的 `timings` 没有被带入。  
    - 证据：`test_latency_jsonl_from_official_path_misses_uia_stage_fields` 失败。延迟目标应按分位数评估，不能把「2 秒内」写成硬指标；但字段不完整就无法做分段评估。

### 低

15. `scripts/selfcheck_uia.py:244-246` 有未使用的占位函数 `UiaPause`；`pyweixin_driver.py:421-423` 有 `if False` 死代码。  
16. dry-run 模拟耗时接近 0 秒，只能证明打点链路，不能代表 Windows 真机耗时。  
17. 适配器没有超长文本分段策略；Fake 路径会整段粘贴。需在真机确认微信单条上限。  
18. `focus_input` / `set_focus` 会抢占前台焦点，发送期间会干扰用户键鼠（文档已提示，但仍需真机观察）。

## 5. 退回修复项

请按优先级修复后通知复测（不要改本报告或已有测试来「迁就」）：

1. 堵住生产绕过路径：`CompositeAdapter.send_text`、应用入口、自检真实发送都必须走 `HumanizedSender` + `RateLimiter` + 运行模式闸门；文档删除或改写「没有绕过入口」。直接操作 `UiDriver` 无法从语言层面禁止，但对外 API 不应让业务代码轻易绕过。  
2. `HumanizedSender` 必须读取 UIA（及 OCR）的 `last_result`：失败 / `paused` / `maybe_sent` 不得记 `outcome=sent`，也不得增加 `sent_count`。  
3. 正式出口为每条待发消息提供稳定 `send_id` 并向下传递；重启与 `maybe_sent` 后不得自动重发，需人工或对账确认。  
4. 在每次 `paste_text` / `press_send` 之前复查 `PauseGate`。  
5. 发送前必须确认当前窗口标题就是目标群（不要只信内存里的 `_current`）；搜索打开后核对标题；重名、同名前缀、搜索失败应返回 `target_not_found`，不得假装成功。  
6. 补齐真实驱动的掉线 / 被踢 / 扫码登录 / 验证码 / 异常弹窗探测；探测不到时保守暂停，而不是当已登录或无弹窗；`verify_sent` 在无可靠接口时保持「未确认」，但上层不得把它当成功。  
7. `_inspect_safety` 对非 `UiaError` 也应转为暂停并返回失败结果，不要让异常直接冒泡。  
8. 校验不确定的首次状态统一为 `maybe_sent`（可保留更细的 error 说明）。  
9. `uia_sent_ids.json` 损坏时应失败关闭或拒绝自动重发，不要静默清空后继续发送。  
10. 拒绝空文本和纯空白。  
11. 若使用剪贴板：先保存用户内容，发送后恢复或清空，并在文档中写明对用户操作的干扰。群名/文本若走 `type_keys`，必须转义特殊字符。  
12. `uia_alerts.jsonl` 与 `pyweixin_driver` 日志一律脱敏后再落盘。  
13. 把 UIA 分段耗时和 `chat_was_open` 写入 `data/latency.jsonl`（或 `extra` / `segments`），供 p50/p95 评估；不要把「2 秒内发出」写成硬失败条件。

## 6. 必须在 Windows 真机验证的项

`PyWeixinDriver` 尚未在真实微信上验收，Linux 补测不能代替下列步骤。建议只用可丢弃的专用小号、家庭网络、已登录的官方 PC 微信，先观察再限频发送（默认 40 条/小时量级）。

1. 当前微信 4.1.x 在开启/关闭「讲述人」时，能否定位主窗口和输入框；`scripts/selfcheck_uia.py` dry-run 能否发现进程、窗口可见、UIA 可达。不要把探测通过当成发送已验收。  
2. 仅在确认语正确时，向「文件传输助手」跑 `--send-test`（不要对会员群试发）。记录 `open_chat` / `paste` / `send` / `verify` / `adapter_total` 以及会话是否已打开；对照目标（已打开规则回复约 3 秒内、其余约 8 秒内，均不含限速层拟人延迟），用分位数观察，不要用单次是否「2 秒内」做硬结论。  
3. 锁屏、最小化到无法操作、远程桌面断会话、掉线、被踢、需扫码登录、验证码或异常弹窗：是否立即写入 `data/PAUSE` 并停止；是否会自动重登或尝试处理验证码。  
4. 人工放置 `data/PAUSE` 后，所有发送是否立刻拒绝。  
5. 预热或保持窗口打开后，向已打开目标群发一条规则回复，适配器自身耗时是否稳定接近 3 秒目标。  
6. **发错群**：同名群、同名前缀（如「交易会员群」与「交易会员群-主群」）、未打开的群、搜索失败、发送过程中手动切到别的会话。必须确认不会发到错误窗口。  
7. 换行、表情、`@`、超长文本、群名含 `+^%{}` 等字符时的实际输入与发送结果。  
8. 若走剪贴板：发送期间和发送后，用户原剪贴板是否被覆盖、能否恢复；是否把群发正文留在剪贴板。  
9. 发送时是否抢占前台焦点，用户正在输入时是否被打断。  
10. `verify_sent` 在真实会话里是否可靠；失败时是否停止重试并留下可对账的 `maybe_sent` 记录。  
11. 专用小号连续观察 7–14 天（限频、夜间静默、先 observe 再小流量 reply），记录是否出现限制登录、频繁掉线或功能限制。不承诺不封号。记录表可用 `tests/observation_log.csv` 与 `tests/WINDOWS_MANUAL_TEST.md`。

## 7. 日志位置

- 补测：`tests/review_b/logs/review_b_pytest.txt`  
- 开发用例：`tests/review_b/logs/test_adapter_uia.txt`  
- 契约 AD-02：`tests/review_b/logs/test_contracts_ad02.txt`  
- 自检 dry-run：`tests/review_b/logs/selfcheck_uia_dryrun.txt`


## 复测

- 结论：**不通过**
- 复测时间：2026-11-11 04:13（Asia/Shanghai，CST，UTC+8）
- 仓库：`/workspace/wechat-community-bot`
- 提交：`b6f2bc3`、`2ea8f3f`、`0146417`、`ed4fe28`、`1a5a6de`、`8fd19a2`（当前工作区还包含其后的 A/C/D/E 提交，一并纳入本次复测）
- 约束：未改 `src/`，未 push，未跑真实界面，自检仅 dry-run；未删除或创建仓库 `data/PAUSE`（复测前后均不存在），补测一律用 `tmp_path` 隔离。新增用例放在 `tests/review_b/test_b_retest_gates_and_driver_boundary.py`。按要求未再做禁止功能关键词与源码脱敏扫描；退回项 9 只复测告警与日志脱敏行为。

首轮 10 项里有 8 项已修好，仍有 2 项未完全达到验收要求，因此适配器 B 复测不通过。未发现博彩 / 结算 / 收款 / Hook 功能。

### 测试结果

| 套件 | 结果 |
| --- | --- |
| `tests/test_adapter_uia.py` | 18 passed |
| 契约 AD-02（`tests/test_adapter_contracts.py -k ad02`） | 3 passed（14 deselected） |
| `scripts/selfcheck_uia.py`（无额外参数，dry-run） | 退出码 0，通过。Linux 上只用 FakeUiDriver；未指定 `--send-test`，无真实发送。自检只读取、未改写仓库 `data/PAUSE` |
| `tests/review_b/` | 50 collected：**2 failed，48 passed**（2 个失败即下方未修项证据） |
| 全量 pytest | **902 passed，3 failed，1 skipped**，共 906 项 |

全量失败 3 项：

- `tests/review_b/test_b_retest_gates_and_driver_boundary.py::test_window_title_spaces_ok_fullwidth_should_normalize`（B 未修项：全半角标题）
- `tests/review_b/test_b_retest_gates_and_driver_boundary.py::test_non_uia_error_during_paste_is_contained_and_pauses`（B 未修项：发送循环未收敛非 UiaError）
- `tests/review_cd/test_retest_fixes.py::test_direct_send_text_must_not_bypass_run_mode`（**不属于 B**：OCR 适配器仍未挂接 `attach_official_sender`，直接 `send_text` 可在 observe 下发出）

开发此前说明 E 仍在修。本轮全量中**未见**模块 E 范围失败，也**未见**适配器 B 官方用例或契约 AD-02 回归。原先会失败的证据用例 `test_direct_adapter_send_bypasses_rate_limiter_and_run_mode` 已改为回归断言：挂接后直接 send 必须被闸门拦截，现已通过。

### 首轮退回项逐条结论

1. **通过** — 直接调用 `adapter.send` / `send_text`、`CompositeAdapter.send_text`、`--send-test` 在已挂接 `HumanizedSender` 时不得绕过 `RateLimiter`、运行模式与 PAUSE；`--send-test` 计入限额。  
   - 实现：`src/adapters/uia.py:368-370`、`455-459`、`727-743`；`src/ratelimit/sender.py:141-142`、`377-404`；`src/adapters/composite.py:91-109`；`scripts/selfcheck_uia.py:209-275`（`HumanizedSender` + `SendGate` + `build_rate_limiter` + `pump`）。  
   - 证据：`tests/review_b/test_b_bypass_and_gates.py:63`（已改为回归断言，observe 下直接 send / send_text / Composite 均被拒，`pressed == []`）；`test_b_retest_gates_and_driver_boundary.py:127`（observe / announce 自动回复被拒，公告可通过；PAUSE 在 `through_official=True` 时仍拒发）、`:158`（正式出口与直接 send 共用每小时限额）、`:199`（`--send-test` 源码走正式出口）。  
   - 残留边界：未挂接 `HumanizedSender` 的单测入口仍可发送（文档已写明，不是生产出口）；调用方可传 `through_official=True` 跳过适配器侧闸门，生产代码中该参数只出现在 `HumanizedSender._invoke_official_send`（`sender.py:402`）。底层 `UiDriver.paste_text` / `press_send` 自身无闸门，见下文。

2. **通过** — `HumanizedSender` 读取 `last_result`，失败 / `maybe_sent` / 幂等跳过不计成功。  
   - 实现：`src/ratelimit/sender.py:352-374`、`457-476`。  
   - 证据：`test_b_bypass_and_gates.py:133`（校验失败时 `sent == 0`、`sent_count == 0`、`outcome != "sent"`）；`test_b_retest_gates_and_driver_boundary.py:345`（`maybe_sent` 同样不计成功）。

3. **通过** — 正式出口传入稳定 `send_id`；幂等文件损坏时隔离、拒发并告警，不静默清空重发；重启后幂等持久化。  
   - 实现：`src/ratelimit/sender.py:386-404`；`src/adapters/uia.py:202-238`、`447-453`、`461-472`。  
   - 证据：`test_b_bypass_and_gates.py:155`（相同文案正式出口只按一次）；`test_b_pause_idempotency.py:158`（重启后 `sent` / `maybe_sent` 均跳过）、`:179` 与 `test_b_retest_gates_and_driver_boundary.py:307`（损坏文件改名为 `uia_sent_ids.json.corrupt`，再次发送被拒，写入 `idempotency_store_corrupt` 告警，`pressed == []`）。

4. **通过** — `paste_text` / `press_send` 之前复查 PAUSE；中途出现则中止。  
   - 实现：`src/adapters/uia.py:522-524`、`530-532`、`541-543`。  
   - 证据：`test_b_bypass_and_gates.py:111`（`focus_input` 时写入 PAUSE 后，不粘贴、不按下，状态 `paused`）。

5. **不通过（部分）** — `read_chat_title` 已做严格标题核对，重名 / 同名前缀 / 中途切群 / 搜索失败 / 空白会拒绝或发到正确窗口；但 **全半角字符未做 NFKC 规范化**。  
   - 已修好：发送路径改用 `read_chat_title`，不再只信 `current_chat` 内存值（`uia.py:501-518`、`533-545`；`pyweixin_driver.py:245-250`、`481-498`）。空白（含全角空格）会压缩后精确匹配（`ui_driver.py:17-26`）。  
   - 证据（通过）：`test_b_send_correctness.py:51`（过期 `current_chat` 不会发进「二群」）；`test_b_retest_gates_and_driver_boundary.py:214`（同名前缀、中途切群、搜索失败均 `target_not_found` 且不按下）。  
   - **未修**：`normalize_chat_title` / `titles_match` 不做 NFKC，与适配器 D 不一致。窗口标题为「交易会员群－主群」（全角连字符）而配置为半角「-」时，`titles_match` 为 False，发送被拒为 `target_not_found`（不会发错群，但会误伤合法会话）。  
   - 证据（失败）：`test_b_retest_gates_and_driver_boundary.py:261`。`PyWeixinDriver.current_chat` 仍只返回内存值（`pyweixin_driver.py:406-407`），发送路径已不再依赖它。

6. **不通过（部分）** — 探活阶段的非 `UiaError`、以及验证码 / 锁屏 / 掉线 / 被踢 / 扫码，会暂停且不重登；但 **发送循环里 paste/press/open 抛出的非 `UiaError` 仍会外溢**。  
   - 已修好：`_inspect_safety` 捕获 `Exception` 并转为暂停（`uia.py:755-768`）；锁屏 / 掉线 / 窗口不可见 / 阻断弹窗写 PAUSE 且不调用 `attempt_login`；`PyWeixinDriver` 无自动重登或打码方法。  
   - 证据（通过）：`test_b_pause_idempotency.py:58`、`:110`、`:216`。  
   - **未修**：`UiaOutboundAdapter.send` 的 `try` 只捕获 `UiaError` 子类（`uia.py:580-614`），`paste_text` 抛出 `RuntimeError` 时异常直接冒泡，不写 PAUSE，也不返回失败结果。  
   - 证据（失败）：`test_b_retest_gates_and_driver_boundary.py:290`。

7. **通过** — 校验不确定首次状态为 `maybe_sent`；空文本 / 纯空白拒发。  
   - 实现：`src/adapters/uia.py:444-445`、`571-578`。  
   - 证据：`test_b_pause_idempotency.py:142`；`test_b_send_correctness.py:79`；`test_b_retest_gates_and_driver_boundary.py:345`。

8. **通过** — 剪贴板降级路径会保存并恢复或清空；`type_keys` 对 `+^%{}()` 转义。  
   - 实现：`src/adapters/pyweixin_driver.py:518-550`、`488-490`、`606-614`。  
   - 证据：`test_b_send_correctness.py:135`、`:152`（已改为回归断言：必须先 `escape_pywinauto_keys`，再 `type_keys(escaped_name)`）。真机恢复效果仍需 Windows 观察。

9. **通过** — `uia_alerts.jsonl` 与 `pyweixin_driver` 日志先脱敏再落盘。  
   - 实现：`src/adapters/uia.py:779-791`；`src/adapters/pyweixin_driver.py` 的 `logger.warning` 均走 `logging_util.redact`（如 `:219`、`:330`、`:550`、`:564`）。  
   - 证据：`test_b_latency_security.py:114`、`:141`。

10. **通过** — 正式出口写入的 `latency.jsonl` 含 UIA 分段字段与 `chat_was_open`。  
    - 实现：`src/ratelimit/sender.py:422-455`（写入 `trace.extra` 与 `session_open`）。  
    - 证据：`test_b_latency_security.py:65`；`test_b_retest_gates_and_driver_boundary.py:323`。延迟仍按分位数观察，不把「2 秒内」写成硬指标。

### 底层驱动无闸门的风险边界

`UiDriver.paste_text` / `press_send`（以及 Fake / PyWeixin 实现）本身不检查限速、运行模式、PAUSE 或幂等。这是驱动层职责边界，不是回归缺陷。风险在于生产代码若直接拿 driver 调用这两步，就会绕过全部闸门。

本轮用 AST + 源码检索确认：`src/` 与 `scripts/` 中，这两步的调用只出现在 `src/adapters/ui_driver.py`、`src/adapters/pyweixin_driver.py`（定义/实现）和 `src/adapters/uia.py` 的 `UiaOutboundAdapter.send`（粘贴/发送前已复查 PAUSE 与标题）。`through_official=True` 在生产代码中只出现于 `HumanizedSender`。证据：`test_b_retest_gates_and_driver_boundary.py:49`、`:113`；既有 `test_b_bypass_and_gates.py:99`。

### 退回修复项（仅余 2 项）

1. `normalize_chat_title` / `titles_match` 应对标题做与适配器 D 类似的 NFKC（全半角）规范化，再严格精确匹配；同名前缀仍不得匹配。避免微信窗口用全角标点时把合法会话误判为 `target_not_found`。  
2. `UiaOutboundAdapter.send` 对打开会话 / 粘贴 / 发送 / 校验过程中的非 `UiaError` 异常，应转为失败结果并写入 PAUSE，不得让异常冲出 `send()`。已按下发送的仍记 `maybe_sent`，不得自动重试。

### Windows 真机仍须验证的项

首轮第 6 节清单仍然有效（`PyWeixinDriver` 仍是未在真实微信上验收的骨架）。请特别确认：

1. 同名群、同名前缀、搜索失败、发送过程中手动切群，是否会发错窗口。  
2. 真实窗口标题的全半角 / 空白形态，是否会把目标群误判为找不到。  
3. 锁屏、掉线、被踢、扫码、验证码或异常弹窗是否立即写 `data/PAUSE` 且不自动重登、不处理验证码。  
4. 剪贴板是否恢复或清空；群名含 `+^%{}` 时搜索是否正确。  
5. `verify_sent` 在真机会话中是否可靠；失败时是否留下可对账的 `maybe_sent`。  
6. `--send-test` 仅在确认语正确时对「文件传输助手」发送，并计入正式限额。  
7. 专用小号连续观察 7–14 天（限频、夜间静默、先 observe 再小流量 reply）。记录表仍用 `tests/observation_log.csv` 与 `tests/WINDOWS_MANUAL_TEST.md`。不承诺不封号。

### 日志位置

- 复测补测：`tests/review_b/logs/review_b_retest_pytest.txt`  
- 全量 pytest：`tests/review_b/logs/full_pytest_retest.txt`  
- 开发用例与契约、自检 dry-run 的复测输出：`tests/review_b/logs/test_adapter_uia_and_ad02.txt`、`tests/review_b/logs/selfcheck_uia_dryrun_retest.txt`

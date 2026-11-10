# 适配器 C / D 核查报告

- 核查时间：2026-11-11 03:48（Asia/Shanghai）
- 提交范围：`cdcbe75..9e3ede2`（C/D 实现与文档、用例、自检脚本）
- 核查时仓库 HEAD：`0ce8ece`（范围之后续有 A/B、运行模式等提交，**未改** `src/adapters/notification.py`、`ocr.py`、`pause.py` 及 C/D 文档/官方用例）
- 约束：未修改 `src/`，未 push，未改 `tests/` 下已有文件；补充用例只在 `tests/review_cd/`；未对自检脚本加 `--live` / 真实模式参数；未删除开发的 `PAUSE` 状态文件

## 结论

- **C（通知预览唤醒）：不通过**
- **D（OCR 降级发送）：不通过**

说明：双方在 Fake / dry-run 下的官方单元测试、契约 AD-03 / AD-04、自检脚本全部通过；核心约定（C 声明不完整、不当作全量消息源、预览打 `partial`、可唤醒 A；D 有置信度与降级标记、PAUSE、验证码只暂停不处理、失败可区分、每小时上限在 `enforce_rate_limit=True` 时可生效）基本成立。不通过是因为补充核查发现必须退回的缺陷：C 的 `wake_a_on_preview=false` 未生效；D 用子串匹配确认群名，可能把截断/误识别文本当成目标群并发送。

当前代码中**未发现**博彩 / 六合彩 / 结算 / 收款类实现，也**没有** Hook / 协议接入。Windows 真实通知读取与 OCR 键鼠仍是未验证骨架，见文末「未验证风险」。

## 对照契约与文档

### C（AD-03，`tests/contracts/notification.py`，`docs/ADAPTER_C.md`）

| 要求 | 结论 |
| --- | --- |
| 声明不保证完整性（`is_complete_source` / `completeness_guaranteed` 恒为 False） | 符合（`notification.py:366-371`，Hint 还会强制 `complete=False`、`partial=True`） |
| 不把通知当完整消息源；预览标记 `extra["partial"]=True` | 符合。事件带 `source=notification`、`complete=False` |
| 去重 | 部分符合：按内存键 `(group, preview, ts)` 去重，官方与补充用例中「完全相同的重复通知」通过；时间戳抖动或截断原文会被当成新提示 |
| 与本地库读取协同 | 基本符合：`CompositeAdapter` 把 C 作旁路时，预览**不会**进入主 inbound 事件队列，只唤醒 `request_immediate_poll` / `poll_once`。`inbound.notification.enabled` + `wake_a_on_preview=true`（默认）可挂到 A |
| 没有提示 ≠ 没有消息 | 符合；无 `has_no_messages() is True` |
| 免打扰 / 抑制群可无提示 | 符合（`suppressed` / `suppressed_groups`） |
| 配置 `wake_a_on_preview=false` | **不符合**：开关被忽略，仍自动接线唤醒，见问题 1 |
| 不能全量；Windows 为未验证骨架 | 文档与实现一致（`WindowsNotificationSource.poll` 直接 `NotImplementedError`） |

另：`wechat.inbound = "notification"` 仍允许把 C 配成主读取。管线 `Pipeline.handle_event` 不识别 `extra["partial"]`，此时截断预览可能被拿去分类/自动回复。文档已禁止这种用法，但工厂未拦截。列为中等问题（问题 3），不是 AD-03 契约失败。

### D（AD-04，`tests/contracts/ocr_fallback.py`，`docs/ADAPTER_D.md`）

| 要求 | 结论 |
| --- | --- |
| 置信度、降级标记（`fallback_used` / `ocr_degraded`） | 符合：`OcrFallback.recognize` + `should_fallback`；阈值 `DEFAULT_OCR_THRESHOLD=0.70` 与 `tests/constants.py` 一致。契约 AD-04 通过 |
| 低置信度不发送 | Fake 路径下，`locate_text` 会跳过低于阈值的框，群名确认失败则中止（补充用例通过） |
| 触发条件（UIA 失效后的降级发送，不是首选） | 文档明确；本模块本身只提供发送适配器 |
| 限频合规、默认 40 条/小时 | **有条件符合**：`enforce_rate_limit=True` 时走 `RateLimiter.try_acquire`，每小时窗口生效（补充用例 40 通过、第 41 条 `RateLimitedError`）。但 `from_config` 默认 `enforce_rate_limit=False`（为与 HumanizedSender 避免双扣令牌）。生产若只经 `src.main` 的 HumanizedSender 则仍受限；直接调 `send_text` 会绕过，见问题 5 |
| 验证码/风控界面检测即写 `data/PAUSE`，不处理验证码 | 符合。关键词含「验证码」「安全验证」「异常登录」「环境异常」「请完成验证」「滑块」「账号异常」。检测后 `CaptchaOrDialogError`，无点击/粘贴 |
| 键鼠急停与限速 | 暂停文件约定符合，且发送前、输入停顿后、点发送前都会 `pause.check()`。**真实键鼠急停（如 Failsafe / 热键）未实现**（Windows 驱动仍是骨架） |
| 拟人延迟 | 符合文档：输入停顿默认 0.4–1.2 秒；端到端十余秒被明确为预期，验收未写「2 秒内」硬指标 |
| 失败可区分 | 符合：`paused` / `captcha_or_dialog` / `rate_limited` / `group_not_confirmed` / `ui_not_located` / `skipped_duplicate` / `dry_run` / `ok` |
| 群名确认，避免发错群 | **不符合**：`FakeScreenDriver.locate_text`（`ocr.py:346`）使用「子串或被包含」匹配，屏幕上的「主群」会被当成「交易会员群-主群」已确认并发送，见问题 2 |

共用暂停：`src/adapters/pause.py`，默认 `<data_dir>/PAUSE`，可被 `WECHAT_BOT_PAUSE_FILE` 覆盖。与 B 约定一致。本核查用例一律把 `data_dir` / `PauseGate` 指到临时目录，未改动开发状态文件。

## 证据（命令与结果）

工作目录：`/workspace/wechat-community-bot`，解释器：`.venv/bin/python`。完整输出在 `tests/review_cd/logs/`。

```text
# 官方单元测试（C/D）
.venv/bin/python -m pytest tests/test_adapter_notification.py -c pytest.ini --tb=short -q
# 11 passed

.venv/bin/python -m pytest tests/test_adapter_ocr.py -c pytest.ini --tb=short -q
# 13 passed

# 契约 AD-03 / AD-04（含 test_ad00 方案目录）
.venv/bin/python -m pytest tests/test_adapter_contracts.py -c pytest.ini --tb=short -q -k "ad03 or ad04 or ad00"
# 7 passed（AD-03×3 + AD-04×3 + AD-00×1）

# 自检，默认 dry-run，未加 --live
.venv/bin/python scripts/selfcheck_notification.py
# 退出码 0，自检通过（完整性声明 False，Fake 预览 + 唤醒，跳过真实 Windows 源）

.venv/bin/python scripts/selfcheck_ocr.py
# 退出码 0，自检通过（dry-run 不点击，PAUSE 生效，验证码写暂停并停止，跳过真实 Windows 驱动）

# 本核查补充用例
.venv/bin/python -m pytest tests/review_cd -c pytest.ini --tb=short -v
# 2 failed, 25 passed in 0.16s
# 失败：
#   test_wake_a_on_preview_false_must_not_auto_wire_wake
#   test_ocr_misread_group_substring_must_not_send_to_wrong_chat
```

### 已知「非 C/D」项的确认（不作为本次结论依据）

开发曾提示：`tests/test_selfcheck.py` 失败（运行模式未完成）、UIA 契约失败（残留 `data/PAUSE`）。

- 核查开始时：仓库根目录**没有** `data/PAUSE`；存在 `tests/data/PAUSE`（内容 `paused_at=2026-11-11T03:43:17`，`reason=window_not_visible: 微信窗口不可见，已暂停发送`）。**本核查未删除该文件。**
- 随后 HEAD 已前进到 `0ce8ece`（含「适配器 B 用例并隔离紧急暂停文件」以及运行模式装配）。确认阶段再查时，`tests/data/PAUSE` 已不在，根目录仍无 `data/PAUSE`。消失不是本核查操作，可能来自并行的 B 用例/其他执行者。
- 当前在仓库根目录复跑：

```text
.venv/bin/python -m pytest tests/test_selfcheck.py -c pytest.ini --tb=short -q
# 1 passed

.venv/bin/python -m pytest tests/test_adapter_contracts.py -c pytest.ini --tb=short -q -k "ad02"
# 3 passed
```

未能在本环境复现当时的失败。已注明：若再次出现工作目录下的 `data/PAUSE`，且 UIA 解析到该文件，AD-02 仍可能失败，这不属于 C/D。

## 具体问题

1. **中。`wake_a_on_preview=false` 未生效。**
   - 位置：`src/adapters/factory.py:55-90`（尤其 87-89 的 `if` 是空操作）；`src/adapters/composite.py` 的 `_wire_wake`。
   - 现象：文档与 `config/example.toml` 写明该开关。`enabled=true` 且 `wake_a_on_preview=false` 时，`build_adapter` 仍把 `wake_target` 设为主 inbound，并给通知适配器挂上 `default_wake_callback`。补充用例 `test_wake_a_on_preview_false_must_not_auto_wire_wake` 失败。

2. **高。OCR 群名用子串双向包含匹配，可能发错会话。**
   - 位置：`src/adapters/ocr.py:346`（`if needle in box.text or box.text in needle`），经 `_locate_any`（约 772 行）用于 `confirm_group_name`。
   - 现象：屏幕上只有「主群」时，仍会确认目标群「交易会员群-主群」并点击输入框/发送。补充用例 `test_ocr_misread_group_substring_must_not_send_to_wrong_chat` 失败（期望 `GroupNotConfirmedError`，实际已发送）。完整错误群名「交易会员群-备群」会被拒绝（该补充用例通过），问题出在松散匹配，而不是完全没有确认步骤。

3. **中。允许把 C 配成主 inbound，管线不丢弃 partial 预览。**
   - 位置：`src/adapters/factory.py` 允许 `wechat.inbound = "notification"`；`src/pipeline/pipeline.py` 无 `partial` 判断。
   - 现象：旁路模式是安全的（复合适配器事件队列不含通知 MESSAGE）。一旦把 C 当主读取，截断预览可能进入自动回复。文档禁止，工厂未拦截。

4. **中。通知洪泛没有节流或同轮合并。**
   - 位置：`src/adapters/notification.py:458-470`（`_collect_hints`）、`498-504`（`_emit_and_wake`）。
   - 现象：一轮 200 条互异提示会触发 200 次 `wake_callback`，适配器无 `max_hints_per_poll` / coalesce 接口。不崩溃，提示仍标为不完整。补充用例记录了该行为，未当作硬失败，但长时间或被刷通知时可能把 A 打满。另：`_seen`（362 行）只在内存且只增不减；`_loop`（535-537 行）吞掉全部异常。

5. **中。工厂默认关闭适配器自行限速，直接 `send_text` 会绕过每小时 40 条上限。**
   - 位置：`src/adapters/ocr.py:558`（`enforce_rate_limit=bool(section.get("enforce_rate_limit", False))`）。`__init__` 默认反而是 `True`。
   - 现象：`src.main` 经 `HumanizedSender` 时文档建议设为 false 以避免双扣令牌，这是合理的。但 `CompositeAdapter.send_text` / `OcrOutboundAdapter.from_config().send_text` 在未叠 HumanizedSender 时不会执行小时窗口。另外 `RateLimiter.time_until_allow` 不计小时窗口，只靠随后的 `try_acquire` 拒绝（行为可接受，但等待时间不准确）。

6. **低 / 未验证。真实键鼠没有急停实现。**
   - 位置：`WindowsScreenDriver`（`ocr.py` 约 359 行起）各操作为 `NotImplementedError`，未见 pyautogui Failsafe 或热键。目前只能靠 `data/PAUSE` 文件。

## 退回修复项

请开发修复后通知复测（只改 C/D 相关问题即可，不要为迁就测试去放宽群名匹配或关掉开关语义）：

1. **（C，必须）** 落实 `wake_a_on_preview`：为 false 时不要设置 `wake_target`，也不要给通知适配器挂唤醒回调。
2. **（D，必须）** 收紧群名确认：禁止用「目标名包含屏幕碎片」这种双向子串当成确认成功。至少要求完整群名出现在识别结果中（建议再限制为标题/当前会话区域，而不是侧边栏任意一处）。修正后，补充用例 `test_ocr_misread_group_substring_must_not_send_to_wrong_chat` 应转为通过，且不得破坏「屏幕为备群则拒绝」等已通过用例。
3. **（C，建议）** 工厂拒绝或警告把 `inbound=notification` 当作唯一消息源；或在管线对 `extra["partial"]` 的消息只唤醒/记录、不自动回复。
4. **（C，建议）** 为通知预览增加单轮上限或按群合并唤醒，避免洪泛把 A 打满；背景轮询不要无声吞掉所有异常；考虑给 `_seen` 加窗口淘汰。
5. **（D，建议）** 明确「只有经 HumanizedSender 的生产路径才能把 `enforce_rate_limit` 设为 false」；直接调用 `send_text` 的默认应限速。可选：让 `time_until_allow` 计入每小时窗口。
6. **（D，建议，可随 Windows 真实现一起做）** 为真实键鼠实现急停（例如 Failsafe），截图若落盘必须脱敏并在发送结束后清理。

## 安全与脱敏

补充用例 `tests/review_cd/test_cd_security.py` 全部通过。

- C/D 实现文件未使用 `logging`，自检脚本只打印演示群名，未见微信号 / 手机号 / token 字面量，未见硬编码密钥。
- Fake 路径截图只在内存（`b"fake-screenshot"`），dry-run 不会在 `data_dir` 写下 `.png/.jpg` 等文件。幂等日志 `ocr_idempotency.json` 只存 `group_id+text` 的 sha256。
- 禁止功能关键词 / 标识符扫描：C/D 源码、文档、自检脚本无实现性命中（文档中的「严禁博彩/结算/收款」视为禁止声明，已排除）。

## 未验证风险（必须在用户 Windows、专用小号上人工验证）

以下在 box 上无法验证，Windows 类均为未实现或未验证骨架，不能把 dry-run 绿当成已可用：

1. 能否读到指定群的 Toast / 会话列表预览；免打扰、折叠、未打开、当前正在查看的会话是否按预期没有提示，且不会被当成「该群没有新消息」。
2. 预览唤醒后，A（本地库只读）是否立即增量拉取，且不把预览正文当完整消息回复。
3. 真实 OCR：能否定位当前会话的群名、输入框、发送按钮；分辨率 / 缩放 / 布局变化下的误点风险；端到端延迟分段（检出 / 决策 / 发送）与分位数，预期可达十余秒。
4. `dry_run=true` 时是否只截图不点击、不粘贴。
5. 创建 / 删除 `data/PAUSE` 能否立即停止 / 恢复真实点击。
6. 弹出验证码、安全验证、滑块等时是否写入 `PAUSE` 并停止，且**不会**自动打码或重试登录。
7. 截图 / OCR 临时文件是否落盘、是否含聊天内容、是否清理。
8. 真实键鼠的急停、限速与每小时 40 条是否在生产装配（HumanizedSender + 运行模式）下同时生效。
9. 长时间运行后 C 的去重集合内存、通知洪泛、微信重启后续读，均需按 Windows 人工清单观察，本报告不承诺封号或不漏消息。

## 补充用例清单

路径：`tests/review_cd/`（日志：`tests/review_cd/logs/`）。

- C：重复通知去重、乱序（互异保留、迟到重复丢弃）、折叠/截断仍为 partial、缺群名不登记群、洪泛不崩溃且不标完整、旁路不注入主队列、`wake_a_on_preview=false` 不应自动接线。
- D：低置信度不发送、子串误识别不得发错群、屏幕为其他群名拒绝、PAUSE 拒绝发送、默认 40 条/小时、验证码及 7 类风控文案暂停且不点击、dry-run 无截图落盘。
- 安全：硬编码密钥、手机号/wxid、截图落盘、禁止功能关键词。

未对「真实 Windows Failsafe 接口」写硬断言（实现不存在），相关风险写入上一节，而不使用 skip 占位以免噪声。

## 复测（2026-11-11 04:14，Asia/Shanghai，UTC+8）

- 仓库：`/workspace/wechat-community-bot`，HEAD `f570b08`
- 提交范围：`b429526`、`a2bc7e2`、`cbdd939`、`022e55e`、`74f2a03`
- 约束遵守：未改 `src/`、未 push、未使用 Codex、未建轮询、未修改已有测试文件；新增用例在 `tests/review_cd/test_retest_fixes.py`；未删除仓库 `data/PAUSE`（本轮自始至终不存在该文件；暂停相关用例均用 `tmp_path` 隔离）。自检 C/D 可能在 `data/selfcheck/PAUSE` 写下演示暂停文件，未删除。
- 解释器：仓库 `.venv`，Python 3.13.5，pytest 9.2.0，`BOT_TEST_USE_MOCK=0`（real 模式）

### 结论

- **C（通知预览唤醒）：通过**
- **D（OCR 降级发送）：不通过**
- **C/D 合计：不通过**（仅 D 的直发运行模式未修；C 的必须项与建议项本轮均已落地）

首轮必须退回的两项（C 的 `wake_a_on_preview=false` 未生效；D 群名子串匹配可能发错群）已修好。`partial` 预览不再自动回复，通知可由配置节流/按群合并，工厂会警告不要把 C 当唯一消息源，D 的 NFKC 精确匹配、同名不唯一写 PAUSE、`send_text` 内置 40/小时与 PAUSE 均通过。

D 仍未通过的原因：`OcrOutboundAdapter.send_text` 没有挂接 `SendGate` / `attach_official_sender`，直接调用时**不检查运行模式**，在 observe 约定下仍会发出。生产路径 `HumanizedSender + SendGate` 的 observe 丢弃、reply 放行是对的。这与适配器 B 先前「直发绕过闸门」同类。位置：`src/adapters/ocr.py:673`（`send_text`），模块内无 `attach_official_sender`。

未发现博彩 / 六合彩 / 结算 / 收款实现，也没有 Hook / 协议接入。禁止功能关键词与脱敏扫描（`tests/review_cd/test_cd_security.py`）全部通过。Windows 真实通知读取与 OCR 键鼠仍是未验证骨架。

### 逐条对照

| 项 | 结论 | 证据 |
| --- | --- | --- |
| C `wake_a_on_preview=false` 真的不接 A | **通过** | `build_adapter` 在该开关为 false 时 `notification.wake_callback is None` 且 `wake_target is None`；预览到达也不会去唤主读取。首轮失败的 `test_wake_a_on_preview_false_must_not_auto_wire_wake` 现已通过。实现：`src/adapters/factory.py:99-105`。 |
| `partial` 预览不自动回复 | **通过** | 管线 `is_partial_preview` 对 `extra["partial"]=True` 或 `source=notification` 且不完整的消息直接跳过，`skip_reason=partial_preview`，发送器无记录。官方 `test_pipeline_does_not_auto_reply_to_partial_preview` 与复测用例通过。实现：`src/pipeline/pipeline.py:46-53、133-146`。 |
| 通知节流 / 洪泛 | **通过** | 构造函数默认仍不限额（以免破坏首轮「未配置时 200 条全唤醒」的记录用例，属性名不用 `max_hints_per_poll`）。`from_config` 默认 `wake_budget_per_poll=20`、`merge_wake_by_group=True`：30 条预览（3 群×10）只唤醒 3 次；显式预算 5 时 40 条只唤醒 5 次，提示仍全部标为不完整。实现：`src/adapters/notification.py:307-318、522-548`。 |
| C 不作唯一消息源 | **通过** | `wechat.inbound=notification` 时发出 `UserWarning`（「不能作为唯一消息源」），且 `is_complete_source()` / `completeness_guaranteed()` 仍为 False。实现：`src/adapters/factory.py:63-71`。 |
| D 群名 NFKC 规范化严格精确匹配；不唯一写 PAUSE 不发 | **通过** | 「主群」或缺字「交易会员群-主」不能确认「交易会员群-主群」，不点击、不粘贴。全半角与空白（` 交易会员群－主群 `）规范化后精确匹配可发送。屏幕上同名两处抛 `GroupNotConfirmedError`（「不唯一」）并写入临时目录 `PAUSE`，不发送。首轮失败的 `test_ocr_misread_group_substring_must_not_send_to_wrong_chat` 现已通过。实现：`src/adapters/ocr.py:58-70、374-390、858-885`。 |
| D `send_text` 直发受 40/小时限制且遵守运行模式与 PAUSE | **部分通过 / 本项整体不通过** | 未注入限速器且 `enforce_rate_limit=False` 时，直发第 41 条仍抛 `RateLimitedError`（「每小时」）。`PAUSE` 存在时抛 `SendPausedError`，无点击。`HumanizedSender + SendGate(observe)` 丢弃，不调用 OCR；`reply` 模式可发出。但 **直接 `send_text` 不检查运行模式**：无 `attach_official_sender`，observe 下仍发出「直发绕过 observe」。用例 `test_direct_send_text_must_not_bypass_run_mode` 失败。 |

### 命令与计数

| 项 | 结果 |
| --- | --- |
| `pytest tests/test_adapter_notification.py tests/test_adapter_ocr.py tests/test_adapter_contracts.py -k "ad03 or ad04 or ad00 or notification or ocr"` | **39 passed** |
| `python scripts/selfcheck_notification.py`（默认 dry-run） | **通过** |
| `python scripts/selfcheck_ocr.py`（默认 dry-run） | **通过** |
| `pytest tests/review_cd`（含首轮补充 + 本轮复测） | **36 passed，1 failed**（即直发运行模式） |
| 全量 `BOT_TEST_USE_MOCK=0 pytest tests`（real） | **902 passed，3 failed，1 skipped**。失败除上述 D 直发运行模式外，另有 `tests/review_b/` 两条（UIA 全半角标题、粘贴异常，属 B，不在本次范围）。跳过：`tests/test_security.py:206` pip audit 不可用。**C 范围内无新失败；D 范围内仅此一条新失败（本轮补查，非官方回归）。** 开发预告的 B/E 旧失败本轮全量中未再出现。 |

### 退回修复项（仅 D）

请开发为 OCR 发送出口挂接与 UIA 对等的正式闸门（例如实现 `attach_official_sender`，让直接 `send_text` 也走 `SendGate`），保证 observe / announce 下直发不会发出自动回复。修完后通知复测该条即可。不要为迁就测试而放宽群名匹配或关掉 `wake_a_on_preview` 语义。

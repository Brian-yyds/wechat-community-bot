# 适配器 B：官方 PC 微信 UI Automation 发送

本适配器（`wechat.outbound = "uia"`，角色名 `roles.OUTBOUND_UIA`）在**已经登录的官方 Windows PC 微信客户端**上，用 UI Automation 把文本发到指定群。思路接近 [Hello-Mr-Crab/pywechat](https://github.com/Hello-Mr-Crab/pywechat) 中的 **pyweixin**（纯 UIA，不注入进程），但本仓库的 `PyWeixinDriver` **尚未在真实微信客户端上验收**，不能把它当作已经跑通的生产实现。

Linux box 只能跑 `FakeUiDriver` 模拟测试。真实发送必须在用户自己的 Windows 电脑上完成。

功能仅限：把已经过限速与规则处理的文本发到群。严禁博彩、六合彩、下注、结算、收款及相关功能。

## 谁来用、怎么配

推荐组合（见仓库根目录 `PLAN.md` 文首结论）：**专用小号 + 家庭网络常驻的 Windows 官方 PC 微信 + 本地只读抓取（适配器 A）+ 本适配器限频发送**。

在 `config/local.toml`（已被 gitignore，不要把密钥写入仓库）中：

```toml
[wechat]
inbound = "mock"          # 或 local_capture（适配器 A）
outbound = "uia"

[outbound.uia]
pause_flag = "data/PAUSE"
max_retries = 2
send_timeout_seconds = 8.0
# 适配器内停顿（计入「适配器自身耗时」，请保持较小）
input_pause_min = 0.12
input_pause_max = 0.35
group_switch_interval_min = 0.35
group_switch_interval_max = 0.70
min_send_interval_seconds = 0.80
# 会话预热 / 保持目标群已打开
keep_chat_open = true
preheat_on_start = true
preheat_groups = ["demo-group"]
return_to_primary_after_send = true
reopen_min_interval_seconds = 30
primary_group = "demo-group"
target_open_chat_seconds = 3.0
target_unopened_chat_seconds = 8.0

[outbound.uia.group_names]
demo-group = "交易会员群演示"
```

`group_id` 会先查 `outbound.uia.group_names`，再查配置里的 `[[groups]]` 名称，最后才把原值当作窗口显示名。

生产路径必须是：管线 / 调度器 → `HumanizedSender`（全局与单群令牌桶、2–6 秒拟人延迟、夜间静默、运行模式与 PAUSE）→ `UiaOutboundAdapter.send_text(..., through_official=True)`。

`HumanizedSender` 构造时会把自身挂到 UIA 适配器（以及 `CompositeAdapter.outbound`）上。之后任何公开 `send` / `send_text`（包括 `CompositeAdapter.send_text`）只要未带 `through_official=True`，都必须再走同一套限速、运行模式与紧急暂停闸门，不能再靠「直接调适配器」绕开。适配器内部另有输入停顿、切群间隔和单次最小间隔，这些时间计入「适配器自身耗时」。

自检 `scripts/selfcheck_uia.py --send-test` 只允许发到「文件传输助手」，必须同时给出确认语，并且：

- 受 `data/PAUSE`（或 `WECHAT_BOT_PAUSE_FILE`）约束，暂停时拒发；
- 走正式 `HumanizedSender` + `SendGate` + `RateLimiter`，**计入限额，不是白名单豁免**；
- 运行模式不允许发送时同样拒绝。

未挂接 `HumanizedSender` 的单测入口仍可做暂停、标题核对与幂等检查，但那不是生产出口，不能当成已经限速。

## 延迟目标

只统计适配器自身耗时，**不含**限速层刻意加入的 2–6 秒拟人延迟：

| 场景 | 目标 |
| --- | --- |
| 目标群窗口已经打开 | 规则回复发出 ≤ 3 秒 |
| 目标群尚未打开 | ≤ 8 秒 |

`send()` 会返回 `UiaSendResult.timings`，分段字段包括 `open_chat`、`paste`、`send`、`verify`、`input_pause`、`group_switch_wait`、`preheat`、`adapter_total`，以及 `chat_was_open`。自检脚本会打印这些数字。

为了逼近「已打开会话 ≤ 3 秒」：请打开 `keep_chat_open` / `preheat_on_start`，让常用目标群尽量保持在前台。预热和间隙回切**不会**放宽限频，并且受 `reopen_min_interval_seconds`（默认 30 秒）约束，避免频繁切换造成异常操作模式。

## 紧急暂停与安全

- 与适配器 D 共用 `src.adapters.pause.PauseGate`：默认开关是 `data/PAUSE`（也可用环境变量 `WECHAT_BOT_PAUSE_FILE`）。文件存在即暂停，不解析内容。
- 需要立刻停手时，在数据目录放一个空的 `PAUSE` 文件即可。
- 窗口不可见、操作系统锁屏、微信掉线、出现验证码或异常弹窗时：**立即创建暂停文件并写入告警**（日志 + `data/uia_alerts.jsonl`），之后所有发送都会失败，直到有人确认环境安全并删除该文件（或调用 `clear_emergency_pause()` / `PauseGate.resume()`）。
- **不会自动反复重登，也不会自动处理验证码。** 掉线后请人工在官方客户端登录。
- 同一 `send_id` 只发出一次；正式出口会传入稳定 `send_id`。若已经按下发送但校验不确定，首次状态为 `maybe_sent` 并拒绝重试，以免刷屏。幂等文件损坏时会隔离旧文件并拒绝自动重发，需要人工对账，不会静默清空后重发。
- 打开会话后会按规范化标题做严格精确匹配；重名、同名前缀或中途切群一律拒发。粘贴和点击发送前会再次检查 PAUSE。
- 剪贴板降级发送后会恢复或清空剪贴板；`type_keys` 会转义特殊字符。告警与驱动日志会脱敏。

## Windows 部署与自检

1. 使用 Python 3.11+，在仓库目录创建虚拟环境并安装 `requirements.txt`。
2. 再安装可选依赖（仅 Windows）：
   - 优先：`pyweixin`（来自 Hello-Mr-Crab/pywechat，跟微信 4.1 较紧，版本随上游变化）；
   - 或者：`pywinauto`。
3. 官方 PC 微信已登录专用小号，**主窗口可见**，不要锁屏，不要让远程桌面断开会话。
4. 部分账号需要先打开「讲述人」，4.x 才暴露较完整的 UI 树（见下方已知限制）。
5. 先 dry-run，不要直接向会员群试发：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_uia.py
```

向「文件传输助手」发一条测试时，必须同时给出确认语（其它目标会被拒绝）：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_uia.py --send-test --confirm yes-send-to-file-helper
```

自检会输出模拟路径的分段耗时，并在 Windows 上检查微信进程、窗口可见性、依赖和 UIA 是否可达。即便探测通过，也不代表真实发送已经验收。

## 专用小号与家庭网络

这些不是可选项，而是公开案例里反复出现的降低风险措施（仍然**不是**零风险）：

- **只用可丢弃的专用小号**，不要用主号或绑定了支付、朋友圈社交的号。用户已接受该号被限制或封号的可能。
- **家庭宽带 + 长期在同一台 Windows 电脑上登录**，保持真实桌面会话。不要放到云主机、机房或与手机不在同一地区的远程环境；远程桌面一旦断会话，UIA 会停。
- 先观察、后发送：限频、夜间静默、日限额都要打开。不要一上来就对多群高频回复。
- 不要批量加好友、拉群、群发私聊。本适配器只发已经过规则过滤的群文本。
- 电脑不要锁屏、不要把微信窗口最小化到无法操作、不要在发送期间用键鼠抢焦点。

## 已知限制（请务必读完）

- 违反《微信个人账号使用规范》（2026-10-15 起生效）中关于第三方自动化、访问或控制微信界面与数据的条款。不存在零封号方案。用户已知悉并接受风险。
- 微信 4.x 大量界面自绘，传统 UIA 大面积失效。wxauto / wxauto4 已实质停更。目前仍在跟 4.1 的主要是 pyweixin，但部分账号要开「讲述人」，拉取聊天有时要十几到四十秒，窗口不可见 / 锁屏 / 远程断连都会停。
- 单靠 UIA **不能**可靠抓取当前没打开的群的全部新消息。全量采集请用适配器 A（本地库只读），本适配器只负责发送。
- `PyWeixinDriver` 对上游 API 做尽力而为的延迟导入与探测，**本仓库未在真实微信客户端上验证**。API 不匹配时会报错，而不会假装发送成功。
- 校验「是否已发出」在真实客户端上可能不可靠；一旦已经按下发送，失败不会自动重试。

## 封号风险自报（摘自 PLAN.md 的公开案例，如实转述）

以下案例来自 2026-11-11 调研文档，不是本仓库的实测，但必须如实告知：

- 2026-08-23，微信 4.1.13.12 + OpenClaw 安装 `pyweixin-rpa` 后立刻退出，并**禁止登录电脑**（[pywechat#296](https://github.com/Hello-Mr-Crab/pywechat/issues/296)）。2026-08-26 另有用户反馈：常用号暂时没事，两个不常用号中一个同样被禁登电脑，另一个隔几天在凌晨被踢下线。
- 2025-12-16，有人问会否封号，回复是「目前没遇到，高频会掉线」（[pywechat#139](https://github.com/Hello-Mr-Crab/pywechat/issues/139)）。这是单点经验，不能当作安全证明。
- 2025-09-07，X 用户 @psssychooo：个人号不支持任何自动化，RPA（如 wxauto）相对「靠谱」但肯定不能商用，可能被封号。
- 2026-03-27 的外部调研曾写「公开经验中几乎没有因纯 UI 自动化被封号的报告」。**该判断已被 2026-06 至 2026-08 的后续案例推翻或部分削弱**，本文以较新反馈为准。
- 《微信个人账号使用规范》禁止第三方自动化；高频收发、异常登录环境会被限制直至永久封号。UIA 比 Hook / 网页协议少一层客户端篡改，但不是护身符。

本项目已具备、且发送时必须走的降低风险措施：单群与全局频率上限、拟人延迟 2–6 秒、夜间静默、同内容冷却、紧急暂停、失败不连重、掉线立即停、不自动重登。这些只能降低概率，不能保证不被处理。

## 需要用户在 Windows 上验证的点

1. 当前微信版本（建议关注 4.1.x）下，讲述人开启与否时能否定位聊天窗口和输入框。
2. `scripts/selfcheck_uia.py` 能否发现进程、窗口并报告 UIA 可达。
3. `--send-test` 向「文件传输助手」发送一条后（须确认语，受 PAUSE 与运行模式约束，并计入限额），分段耗时是否接近目标（已打开 ≤3s，未打开 ≤8s，均不含限速层延迟）。
4. 锁屏、最小化、远程桌面断连、掉线、弹出验证码时，是否立即暂停且不会自动重登。
5. 紧急暂停文件 `data/PAUSE` 是否立刻阻止发送。
6. 预热后，向已打开的目标群发规则回复，适配器自身耗时是否稳定落在 3 秒内。

验证通过前，请把 `wechat.outbound` 保持为可逆配置，并只用专用小号。

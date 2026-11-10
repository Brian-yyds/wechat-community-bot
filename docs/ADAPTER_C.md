# 适配器 C：通知 / 会话列表预览（辅助唤醒）

角色名：`wechat.inbound = "notification"`（`src.adapters.roles.INBOUND_NOTIFICATION`）。

本适配器**不是全量消息源**。它只在系统通知或会话列表出现预览时，产出一条不完整事件（群名、发送者、预览文本、时间），并标记 `extra["partial"] = True`，表示正文可能被截断。主要用途是：**唤醒或催促 A 适配器（本地库只读抓取）立刻轮询**。

## 能力与限制

能做：

- 把「可能有新消息」变成管线可消费的预览事件；
- 通过 `wake_callback` / CompositeAdapter 的 `wake_target` 通知 A 立即拉取；
- 在 box 上用 `FakeNotificationSource` 做注入测试。

不能做、也不得被配置成：

- 全量抓取（正在查看的会话通常不再弹通知；免打扰 / 关闭通知的群会漏；没有可靠历史回溯）；
- 把「本轮没有提示」当成「该群没有新消息」；
- 用预览正文代替完整消息去做精确回复（文本可能被系统截断）。

`is_complete_source()` 与 `completeness_guaranteed()` 恒为 `False`。

## 配置

在 `config/local.toml` 中（示例见 `config/example.toml`）：

```toml
[wechat]
inbound = "notification"   # 或保持 local_capture / mock，把通知当作辅助
outbound = "mock"

# 亦可写在 [wechat.notification]
[inbound.notification]
# fake：box / 单测；windows：仅 Windows，且仍是未验证骨架
source = "fake"
poll_interval_seconds = 1.0
# 免打扰或明确不需要唤醒的群名
suppressed_groups = []
# 当读取侧是 A 时，建议打开，收到预览就催促 A 轮询
wake_a_on_preview = true
```

也把通知当作「辅助旁路」、主读取仍走 A 时，保持 `wechat.inbound = "local_capture"`，并设置：

```toml
[inbound.notification]
enabled = true
wake_a_on_preview = true
```

`build_adapter()` 会把通知适配器挂到 `CompositeAdapter.notification`，并把唤醒钩子接到主读取适配器的 `request_immediate_poll(hint)`（A 应实现该方法；未实现则忽略）。

## 部署（Windows）

1. 官方 PC 微信保持登录，Windows 通知权限对微信开启。会话列表预览模式还需要窗口可见，不能锁屏或断开桌面会话。
2. 本机创建虚拟环境并安装项目依赖（见 `docs/WINDOWS.md`）。
3. 复制示例配置为 `config/local.toml`，按上面填写。真实密钥不要入库。
4. 先跑自检（默认 dry-run，不监听真实通知流）：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_notification.py
```

5. `WindowsNotificationSource` **尚未在真实微信 4.x 上验证**。需要读取系统 Toast 时，另行安装 `winrt` 或 `winsdk`；需要读会话列表预览时，另行安装 `uiautomation`。安装依赖 ≠ 已经可用。缺依赖或在 Linux 上实例化会给出明确错误，而不会在 import 阶段崩溃。

## 风险自报

- 协议 / 接入方式：Windows 系统通知中心或官方客户端会话列表 UI 预览（只读）。不注入微信进程，不读本地消息库，不使用 Hook / iPad / Web 协议。
- 是否容易被官方检测：相对 Hook 与协议登录更低，但仍可能违反个人微信自动化 / 辅助工具相关条款；公开项目很少，不能当成「安全」。
- 完整性风险：这是最大的产品风险。免打扰、当前会话、关闭通知、图片/语音都可能没有任何可用正文。必须与 A 组合，不能单独承担全量抓取。
- 封号风险：纯旁路读取、不做发送时，主要风险是条款与稳定性，而不是点击模式。本适配器不发送消息。
- 验证状态：Windows 真实源为未验证骨架。box 上只跑 Fake。需要用户在 Windows 电脑上验证：能否收到指定群的 Toast / 会话列表预览、免打扰是否按预期被抑制、唤醒回调是否让 A 立即轮询。

严禁把本适配器用于博彩、结算、收款或任何被禁止的功能。

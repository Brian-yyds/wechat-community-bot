# 适配器 A：本地库只读增量抓取

本适配器在用户 Windows 电脑上，对官方 PC 微信 4.x 已经同步到本机的消息库做**只读**增量监听，把配置白名单内（交易会员群）的全量消息转为统一 `Message` 事件。

功能范围只包括：实时监控群消息、消息整理识别所需的采集。发送走其他适配器（Mock / 后续 UIA）。严禁博彩、结算、收款相关功能。

box 上只用模拟库做测试；真实微信只能在用户 Windows 电脑上运行，并须用自检脚本验证表结构映射。

## 风险自报（请先阅读，用户已知悉并接受）

个人微信没有官方 API。《微信个人账号使用规范》（2026-10-15）禁止通过非官方第三方软件访问、读取或控制微信界面与数据。因此**本方案不是零封号、也不是官方授权路径**。

调研结论（见仓库 `PLAN.md` 文首，此处只摘要，不展开解密或密钥提取步骤）：

- 本地库只读的检测面小于 Hook / iPad 协议，但**不是零风险**。2026-06-17 已有用户公开反馈：用 `wx-cli` 做本地 WCDB 解密统计，当天就被提示「使用非官方客户端/外挂」并风控封号（https://x.com/Meta8Mate/status/2067119572633989470）。另有 2026-05-28 的类似提醒。
- 腾讯于 2026-01 通过 GitHub DMCA 批量下架解密/导出类仓库（公开记录处理了含父仓库在内的数千个仓库网络）。PyWxDump 作者亦于 2025-10-20 因律师函删库。即使「只读自己的记录」，腾讯已把此类工具定性为规避技术保护措施。
- 本仓库**不内置密钥提取、不写解密步骤、不注入进程、不 Hook**。密钥只能由用户自行放到环境变量或本地文件；适配器只读打开用户提供的已解密 SQLite 副本或明文库。这并不能消除封号与合规风险。
- 建议：只用**可丢的专用小号**，不用主号或支付号；固定**家庭网络**与固定设备，不要放云主机；先只读观察 7–14 天，确认没有下线、「环境异常」或禁登电脑后，再考虑打开发送。

若不能接受上述残余风险，请不要启用 `wechat.inbound = local_capture`，并接受无法保证全量抓取。

## 工作方式与边界

- **严格只读**：以 SQLite `mode=ro` 只读打开，游标写在本仓库 `data/`，绝不写微信原库。
- **增量游标**：按消息自增入库序（`create_seq` 或映射列）而不是只按时间戳，避免乱序漏抓。游标与已见 `msg_id` 持久化，重启不重复、不丢。
- **全量覆盖免打扰 / 折叠 / 未打开的群**：只以「群白名单 + 消息表游标」为准，不看会话是否打开、是否提醒。前提是官方客户端已经把这些群的消息同步到本机库。
- **轮询**：默认 1 秒，带 ±0.1 秒抖动，目标是增量检出延迟尽量小于 1 秒。每条事件带 `detected_at`，便于打点（`detected_at - 消息时间`）。
- **自己发的消息**：不丢弃，只打 `is_self` 标记，由管线决定不回复。
- **非文本**：图片、语音等记录类型占位，保证「有过这条消息」可对账。
- **库不存在**：优雅降级（本轮无新消息并打日志），不崩溃。
- **库被占用 / 版本或文件变化 / 微信重启**：短锁定会重试或给出可区分的锁定错误；文件被替换、句柄丢失、游标回退时自动重新打开并续读，靠 `msg_id` 去重避免重放。
- **对账**：`src.adapters.local_db.reconcile` 可按时间窗对比已发出事件与库内消息，报告缺口；`catch_up` 按缺口 ID 补抓。

## 配置项

在 `config/local.toml`（已被 gitignore）中把 `wechat.inbound` 设为 `local_capture`，并填写 `[inbound.local_capture]`：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `db_path` | 空（或环境变量 `WECHAT_LOCAL_DB_PATH`） | 已解密 SQLite 路径。不要把真实库或密钥提交进 Git。 |
| `cursor_path` | `data/local_capture_cursor.json` | 游标持久化位置。 |
| `poll_interval_seconds` | `1.0` | 轮询间隔。 |
| `poll_jitter_seconds` | `0.1` | 间隔抖动。 |
| `poll_timeout` | `2.0` | 只读连接忙等待超时。 |
| `lock_retries` / `lock_retry_delay_seconds` | `10` / `0.03` | 库锁重试。 |
| `group_whitelist` | 可回落到 `[[groups]]` | 只抓这些群 ID。 |
| `self_ids` | 可回落 `bot.identity.self_ids` | 用于 `is_self`。 |
| `require_key` / `encrypted` | `false` | 为 true 且未配置密钥时启动失败，错误信息清晰且不含密钥。 |
| `schema.*` | 对齐本仓库模拟库 | 表名/列名映射。真实 4.x 结构**必须在用户 Windows 上用自检验证后修改**。 |

密钥（如使用加密库并由用户自行提供）：

- 环境变量 `WECHAT_LOCAL_DB_KEY`，或
- `WECHAT_LOCAL_DB_KEY_FILE` 指向仅当前用户可读的本地文件。

本 MVP **不会**用该密钥在代码里演示解密步骤；默认请提供已经解密的副本。日志会脱敏，不会打印密钥。

发送侧保持 `wechat.outbound = mock`（或后续 UIA），由 `build_adapter` 组合成 `CompositeAdapter`。

## 在用户 Windows 上的部署与自检

运行环境：长期保持桌面会话的 Windows 10/11，登录**专用小号**的官方 PC 微信 4.x，出口为常用家庭网络，不要用云主机或频繁换机。

1. 安装 Python 3.11+（勾选 Add to PATH），进入本仓库：

```powershell
cd C:\path\to\wechat-community-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config\example.toml config\local.toml
```

2. 编辑 `config\local.toml`：

   - `wechat.inbound = "local_capture"`
   - 填写 `inbound.local_capture.db_path` 为你准备好的**已解密 SQLite 路径**（或先用自检的 `--simulate` 跑通流程）
   - 填写交易会员群的 `group_whitelist` 与本小号 `bot.identity.self_ids`
   - 按自检结果修正 `schema` 映射
   - 不要把密钥写进配置文件

3. 先跑模拟端到端（不触达真实微信库）：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_local_capture.py --simulate
```

4. 再对真实路径做只读打开、游标与表结构检查（仍不会写原库；若表名/列名不匹配，脚本会明确失败并提示改映射）：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_local_capture.py --config config\local.toml
```

也可：`.\.venv\Scripts\python.exe -m pytest tests/test_adapter_local_capture.py tests/test_adapter_contracts.py -k "ad01 or local_capture or local_db"`

5. 建议先只读观察 7–14 天。出现强制下线、验证码、「环境异常」或禁止登录电脑时，立刻停止并通知，不要自动反复重登。

## 专用小号与家庭网络注意事项

- 只用可丢的专用小号，主号、支付号、实名生活号一律不上机。
- 固定设备、固定家庭出口 IP；不要云服务器异地扫码常驻。
- 新号不要当天挂自动化，先人工正常聊几天到一两周。
- 本适配器只读；在观察期不要打开自动回复或高频公告。
- 密钥、聊天库、游标文件不得上传网盘或提交 Git（`data/`、`config/local.*`、`*.key` 已忽略）。
- 不要用管理员权限无必要地运行。

## 需要在用户 Windows 上验证的点

下列事项在 Linux box 上只能用模拟库覆盖，**必须**在已登录官方 PC 微信的 Windows 上确认：

1. `db_path` 是否指向当前 4.x 版本可只读打开的库（或你准备的解密副本），只读打开是否被客户端文件锁长时间挡住。
2. `schema` 表名/列名是否与当前小版本一致（群 ID 是否为 `xxx@chatroom`，入库序列、时间戳格式、消息类型取值）。
3. 免打扰、折叠、当前未打开的交易会员群，只要客户端已同步，文本消息是否都能在数秒内被增量检出（目标检出延迟尽量 <1s）。
4. 微信重启、库文件被替换或短暂锁定后，游标能否续读且不漏不重。
5. 自己在群里发的消息是否带 `is_self`；非文本是否留下类型占位。
6. 观察期内账号是否出现下线或风控提示。

## 相关接口（开发）

- `LocalDbReader`：`poll` / `iter_new` / `get_cursor` / `set_cursor` / `fetch_by_ids` / `assert_readonly`
- `DbReader` / `SqliteDbReader`：只读读取已解密 SQLite
- `DbKeyProvider` / `EnvFileKeyProvider`：可插拔密钥
- `LocalCaptureAdapter`：`InboundAdapter` 实现，`from_config`，后台轮询
- `reconcile` / `catch_up`：对账与补抓
- `build_adapter({"wechat": {"inbound": "local_capture", "outbound": "mock"}, ...})`

详情见 `docs/ARCHITECTURE.md`、`docs/PROTOCOL_RISK.md`、`docs/WINDOWS.md`。

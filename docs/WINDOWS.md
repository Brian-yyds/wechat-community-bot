# Windows 部署说明

第一阶段只跑本地模拟适配器，**不登录微信、不接入真实协议**。本仓库使用 `pathlib` 管理路径，不依赖 Linux 专有命令或设备文件，可在 Windows 10/11 上运行。

个人微信专用小号的真实协议适配（A 本地库只读抓取、B UIA 发送、C 通知预览唤醒、D OCR 降级发送）将在后续任务交付。当前请把 `wechat.inbound` 与 `wechat.outbound` 保持为 `mock`，二者也可以在配置里分开写，为以后「A 读 + B 发」预留组合方式。

## 环境

1. 安装 [Python 3.11 或更高版本](https://www.python.org/downloads/)（安装时勾选 “Add python.exe to PATH”）。
2. 打开 PowerShell，进入本仓库目录：

```powershell
cd C:\path\to\wechat-community-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

若执行策略限制脚本，可对当前用户放行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

也可以不激活虚拟环境，直接调用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

```powershell
Copy-Item config\example.toml config\local.toml
```

用编辑器打开 `config\local.toml`：

- 填写 `bot.identity` 中本小号的 wxid 与群内昵称，避免回复自己；
- 按需调整 `rate_limit.global_replies_per_hour`（默认 40）以及单群 / 每分钟上限；
- 夜间静默默认开启（00:30–07:00），静默期自动回复会被丢弃、不会排队补发；
- 不要把密钥写入仓库。真实配置文件 `config\local.*` 已被 gitignore。

数据目录默认为仓库下的 `data\`（已被忽略），用于 JSONL 记录、去重状态与调度状态。程序会自动创建。

## 自检与演示

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck.py
.\.venv\Scripts\python.exe -m src.main --demo
.\.venv\Scripts\python.exe -m pytest
```

自检会检查 Python 版本、依赖、配置能否加载、数据目录是否可写、限速参数是否合理，并跑一遍模拟端到端（不触达微信）。

## 注意事项

- 不要用管理员权限无必要地运行本程序。
- 路径请一律使用 Python 的 `pathlib.Path`，避免手工拼接反斜杠。
- 当前接受封号风险是后续接入真实协议时的产品决策；第一阶段 Mock 不登录，没有真实封号路径。接入前请更新 `docs/PROTOCOL_RISK.md`。
- 严禁配置或实现博彩、六合彩、下注、结算、收款相关功能。

# Windows 部署说明

第一阶段只跑本地模拟适配器，**不登录微信、不接入真实协议**。本仓库使用 `pathlib` 管理路径，不依赖 Linux 专有命令或设备文件，可在 Windows 10/11 上运行。

适配器 MVP：A 本地库只读抓取见 `docs/ADAPTER_A.md`；C 通知预览唤醒见 `docs/ADAPTER_C.md`；D OCR 降级发送见 `docs/ADAPTER_D.md`。B UIA 发送见 `docs/ADAPTER_B.md`（若已交付）。C/D 的 Windows 真实路径仍是未验证骨架，box 上请用 Fake 源/驱动，OCR 先保持 `dry_run = true`。启用前请阅读对应风险自报，并运行 `scripts/selfcheck_local_capture.py`、`scripts/selfcheck_notification.py`、`scripts/selfcheck_ocr.py`（后两者默认 dry-run，不点击真实窗口、不监听真实通知流）。默认示例配置仍是 mock。

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

- 运行模式 `run.mode` 默认为 `observe`（只读不发）。专用小号实测自动回复时，把这一行改成 `reply`；若只想发定时公告则改成 `announce`。不必改代码。
- 填写 `bot.identity` 中本小号的 wxid 与群内昵称，避免回复自己；
- 按需调整 `rate_limit.global_replies_per_hour`（默认 40）、`group_replies_per_hour`（默认 20）、`global_per_minute`（默认 8）、`group_per_minute`（默认 3）；
- 拟人随机延迟按场景分档：其余/未打开会话默认 2–6 秒，已打开会话的规则回复默认 1.5–2.4 秒（对准 3 秒端到端，下限默认 ≥1.5 秒，可配），公告默认同其余档；夜间静默默认开启（00:30–07:00），静默期自动回复会被丢弃、不会排队补发；
- 不要把密钥写入仓库。真实配置文件 `config\local.*` 已被 gitignore。

数据目录默认为仓库下的 `data\`（已被忽略），用于 JSONL 记录、延迟打点、去重状态、调度状态、限速小时窗口与运行模式状态。程序会自动创建。

紧急暂停：在数据目录放置一个名为 `PAUSE` 的文件后，任何发送都不会执行（与适配器 B/D 约定一致）。删除该文件即可恢复。

## 自检与演示

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck.py
.\.venv\Scripts\python.exe scripts\selfcheck_local_capture.py --simulate
.\.venv\Scripts\python.exe scripts\selfcheck_notification.py
.\.venv\Scripts\python.exe scripts\selfcheck_ocr.py
.\.venv\Scripts\python.exe -m src.main --demo
.\.venv\Scripts\python.exe -m pytest
```

`selfcheck.py` 检查 Python 版本、依赖、配置、数据目录与限速，并跑一遍模拟端到端（不触达微信），同时输出延迟分段耗时。`selfcheck_notification.py` / `selfcheck_ocr.py` 默认 dry-run，只用 Fake 源/驱动，不会监听真实通知或点击微信窗口。

## 注意事项

- 不要用管理员权限无必要地运行本程序。
- 路径请一律使用 Python 的 `pathlib.Path`，避免手工拼接反斜杠。
- 当前接受封号风险是后续接入真实协议时的产品决策；第一阶段 Mock 不登录，没有真实封号路径。接入前请更新 `docs/PROTOCOL_RISK.md`。
- 严禁配置或实现博彩、六合彩、下注、结算、收款相关功能。


## 适配器 B（UIA 发送）

真实 UIA 只能在本机已登录的官方 PC 微信上运行。请先阅读 [ADAPTER_B.md](ADAPTER_B.md)，再执行：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_uia.py
```

默认 dry-run，不会向群发送。向「文件传输助手」试发必须同时给出确认语，见该文档。

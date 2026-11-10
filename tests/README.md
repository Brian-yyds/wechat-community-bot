# 测试目录说明

本目录是「交易会员群」本地微信群机器人的核查测试资产。产品功能仅四项：实时监控群消息、群消息整理识别、自动回复、定时公告。

主路线：官方 PC 微信 + 本地库增量只读 + UIA 限频发送；C 通知辅助、D OCR 降级。不做 Hook / 非官方协议号。
封号相关只验限频合规与观察记录，**不承诺不封号**。

**严禁**博彩、六合彩、下注、结算、收款及相关功能。`data/negative_forbidden.jsonl` 中的相关内容只作为「必须拒绝并上报」的负向用例，**不是**需求。

不在此目录创建任何轮询或定时例程。测试按需手动执行。

## 如何运行

依赖：Python 3，以及 `pytest`。`./run_all.sh` 会在 `tests/.venv` 里自动创建虚拟环境并安装 pytest（避免系统 Python 受 PEP 668 限制）。也可手动：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`。

```bash
cd tests

# 对接开发模块（默认）。模块未交付时相关用例 skip，不会把框架本身打成错误。
./run_all.sh
# 或
./run_all.sh real

# 仅跑脚手架自检（加载 tests/mock，不是产品实现，不能当作验收通过）
./run_all.sh mock

# 追加 pytest 参数
./run_all.sh real -k security
./run_all.sh mock tests/test_classification.py -q
```

也可直接：

```bash
cd tests
python3 -m pytest -c pytest.ini
BOT_TEST_USE_MOCK=1 python3 -m pytest -c pytest.ini
```

跑完后执行 `python3 report.py`（`run_all.sh` 已包含）会写入 `REPORT.md`。

环境变量：

| 变量 | 含义 |
| --- | --- |
| `BOT_TEST_USE_MOCK=1` | 使用 `tests/mock` 跑通用例逻辑 |
| `BOT_ADAPTER_MODULE=src.bot_api` | 指定开发模块路径 |
| `BOT_TEST_LATENCY_OPEN_P95` | 规则回复且会话已打开的端到端 P95 目标（默认 3） |
| `BOT_TEST_LATENCY_OTHER_P95` | 其余场景端到端 P95 目标（默认 8） |
| `BOT_TEST_HUMANIZE_OPEN_MIN` / `_MAX` | 打开会话短拟人区间（默认 1.0–2.2） |
| `BOT_TEST_HUMANIZE_DEFAULT_MIN` / `_MAX` | 默认拟人区间（默认 2–6，对 8s 路径） |

长时稳定性（24h/72h 内存与句柄）不在默认 pytest 门禁里，按 `ACCEPTANCE.md` 的 ST-05/ST-06/ST-07 在预发执行。

压测序列：

```bash
python3 generators/generate_load.py burst --n 200
python3 generators/generate_load.py spam --n 40
python3 generators/generate_load.py mixed --n 300
```

输出默认在 `data/generated/`（已忽略，不入库）。重新生成常规数据集：`python3 data/build_dataset.py`。

## 如何阅读报告

`REPORT.md` 含：总结论（通过 / 不通过 / 未测）、各文件通过/失败/跳过计数、失败用例的具体断言、跳过原因、**退回开发的修复项**、以及 git 状态（本测试流程**不会**擅自 push）。

- 失败：退回开发，修复后复测。
- 跳过：通常是模块未交付或接口未对齐，不算通过。
- mock 模式全绿：只说明测试脚手架自洽，**不能**代替产品验收。

## 给开发的接口约定

请实现一个模块（推荐 `src/bot_api.py`，或通过 `BOT_ADAPTER_MODULE` 指定），对外提供下列接口。详细签名见 `adapter.py` 文件头注释。

1. **分类** `classify(message) -> dict`  
   - 入参：`id, group, sender, ts, text`，可选 `quote`、`mentions`。  
   - 出参：`category, should_reply, reply, flags`。  
   - `category` 只能是：`提问`、`行情讨论`、`广告刷屏`、`公告回复`、`闲聊/无关`、`需人工`；禁止类为 `禁止功能-上报`，且 `flags` 含 `forbidden_report`。  
   - 回复策略与禁止短语见 `ACCEPTANCE.md` B 节。不得给出买卖建议或承诺收益。

2. **调度** `Scheduler`，**必须可注入时钟**（`clock.now() -> datetime`，`set_clock`）。  
   - `add_announcement(job_id, content, run_at, group)`  
   - `tick(now=None)`：到期且未发送的任务。  
   - `mark_sent(job_id, sent_at)`  
   - `on_recover(now=None)`：重启/掉线补发；已发送不补，窗口外跳过，窗口内只补一次。默认窗口 10 分钟。

3. **发送层** `Sender.send(group, text, *, kind=...)`，测试可替换为记录型 mock，不触达真实微信。

4. **限流** `RateLimiter.allow / record / next_delay / in_night_silence`  
   单群与全局限频同时生效；默认全局 40 条/小时（可配）；超限自动回复降级为不回；夜间静默不主动发公告；同内容冷却；`next_delay` 带抖动。

5. **拟人延迟** `humanize_delay_range(path=None) -> (下限秒, 上限秒)`  
   默认路径（其余场景，P95 目标 8s）建议 2～6 秒。`path='open_session_rule'` 为打开会话的规则回复提供短区间（建议 1.0–2.2s），与 P95 目标 3s 相容。默认 2–6s 与 3s 目标冲突，不能混用。阈值可配。另建议提供 `humanize_delay(text, rng=, path=)`。

6. **日志脱敏** `redact_log(line) -> str`：掩码手机号、微信号、wxid、token。

7. **运行模式** `RunModeController`：`observe`（只收不发）、`announce`（只发公告）、`reply`（才自动回复）。提供 `get_mode` / `set_mode` / `allow_auto_reply` / `allow_announcement`，状态可放入 store，切换立即生效，重启保持。

模块缺失时测试会 skip 并写明原因。安全扫描与禁止功能扫描不依赖模块，会直接扫描 `src/`、`config/`、`templates/` 等。

另外请在 `docs/PROTOCOL.md` 或配置项 `wechat.protocol` **自报**实际使用的微信协议/框架（Hook / Web / iPad / Windows / Mac 等）及已知风险，否则 RK-01 在有源码后会判失败。

## 目录

| 路径 | 说明 |
| --- | --- |
| `ACCEPTANCE.md` | 验收清单与测试方案 |
| `WINDOWS_MANUAL_TEST.md` | 用户 Windows 人工实测清单（不承诺不封号） |
| `observation_log.csv` | 7–14 天观察记录表模板 |
| `adapter.py` | 适配层 |
| `contracts/` | 各方案适配器契约套件 |
| `sim/` | 模拟微信本地库与对账 |
| `data/messages.jsonl` | ≥120 条虚构群消息 |
| `data/negative_forbidden.jsonl` | 禁止功能负向用例 |
| `data/build_dataset.py` | 数据集生成脚本 |
| `generators/generate_load.py` | burst / 刷屏序列生成器 |
| `mock/` | 仅脚手架自检用，**不是**产品实现，禁止部署 |
| `test_*.py` | 自动化用例 |
| `report.py` / `run_all.sh` | 执行与汇总 |
| `REPORT.md` | 最近一次运行报告（生成物） |

## 注意事项

- 请勿修改本测试去「迁就」禁止功能。发现相关实现须立即报告智哥。
- 根目录 `.gitignore` 现有 `data/` 规则会忽略 `tests/data/`。建议追加：

  ```
  !tests/data/
  !tests/data/**
  ```

  或使用 `git add -f tests/data/*.jsonl`。
- 全部消息为虚构，不含真实人名或手机号。脱敏测试里的号码与 wxid 同样是假数据。

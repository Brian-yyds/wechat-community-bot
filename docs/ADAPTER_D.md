# 适配器 D：OCR 降级发送

角色名：`wechat.outbound = "ocr"`（`src.adapters.roles.OUTBOUND_OCR`）。

这是 **UIA 发送（方案 B）失效时的降级路径**，不是首选。流程是：截图 → OCR 定位当前群名 / 输入框 / 发送按钮 → 点击、粘贴、再点发送。公开反馈里这条路径**一条消息经常要等十余秒**，定位还受分辨率、缩放、窗口位置和布局变化影响，不能当作日常发送主干。

## 能力与限制

能做：

- 在 box 上用 `FakeScreenDriver` / `FakeOcrEngine` 验证限速、暂停、幂等与定位逻辑；
- 强制检查紧急暂停文件、异常弹窗 / 验证码，以及同一群同一文案的幂等窗口；
- 发送前增加输入停顿。

不能做：

- 采集群消息（只发送，不读取）；
- 自动识别并填写验证码、滑块或绕过异常弹窗；
- 保证发到正确会话——群名按规范化（去空白/全半角/大小写）后的**严格精确匹配**确认，屏幕上只有「主群」不会匹配「交易会员群-主群」；匹配不唯一或不确定则写入 `PAUSE` 并中止；
- 在无桌面会话、锁屏、最小化到不可截屏的情况下工作。

## 必须遵守的安全约定

1. **限速与运行模式**：生产路径必须继续走 `HumanizedSender`（现有 `src/ratelimit` 层）。`HumanizedSender` 构造时会把自身挂到 OCR 适配器（以及 `CompositeAdapter.outbound`）的 `attach_official_sender` 上。之后任何公开 `send_text` 只要未带 `through_official=True`，都必须再走同一套限速、运行模式与紧急暂停闸门。`HumanizedSender` 完成判定后以 `through_official=True` 进入实际 OCR 操作，适配器不再二次扣令牌。`enforce_rate_limit=true` 仅用于未挂接正式出口时自行获取令牌；与 HumanizedSender 叠用同一限速器时请保持 `false`。**无论该开关是否打开**，`send_text` 都会执行适配器内的每小时上限（默认 40 条，可配 `global_replies_per_hour` 或沿用 `rate_limit.global_replies_per_hour`）。未挂接正式发送器、也未注入 `SendGate` 时，直接 `send_text` **默认拒发**（未知运行模式等同 observe），并写不含正文的脱敏日志；仅测试可设 `allow_ungated_for_tests=true` 放行。
2. **紧急暂停**：与 B（UIA 发送）使用同一约定，见 `src/adapters/pause.py`。默认文件是 `<data_dir>/PAUSE`（可用环境变量 `WECHAT_BOT_PAUSE_FILE` 覆盖）。文件存在就停止一切点击和粘贴。一键暂停：创建该空文件。恢复：人工确认后删除。
3. **验证码 / 异常弹窗**：OCR 或屏幕文字命中「验证码」「安全验证」「异常登录」「环境异常」「请完成验证」「滑块」「账号异常」等时，立即写入 `PAUSE` 并停止，等待人工处理。没有自动打码或重试登录。
4. **幂等**：同一 `group_id` + 同一文案在配置的时间窗内（默认 600 秒）不重复发送，状态写在 `data/ocr_idempotency.json`，重启后仍生效。
5. **输入停顿**：在截图定位前后会随机停顿（默认 0.4–1.2 秒）。再加上 HumanizedSender 的 2–6 秒拟人延迟，以及截图与 OCR 本身的耗时，端到端延迟达到十余秒是预期现象，不是故障。

## 配置

```toml
[wechat]
outbound = "ocr"

[outbound.ocr]  # 也可写在 [wechat.ocr]
# fake：box / 单测；windows：仅 Windows，且仍是未验证骨架
driver = "fake"
# tesseract 或 rapidocr（仅 windows 驱动使用）
engine = "tesseract"
dry_run = true
confirm_group_name = true
input_pause_min = 0.4
input_pause_max = 1.2
idempotency_window_seconds = 600
# 直接调用 send_text 时是否自行扣令牌；经 HumanizedSender 时建议 false
enforce_rate_limit = false
# global_replies_per_hour = 40  # 直接 send_text 也会执行
# allow_ungated_for_tests = false  # 仅测试；生产保持关闭
# pause_file = "data/PAUSE"
```

群显示名用于「发送前确认当前会话」，可写在 `[[groups]]` 的 `name` 里，或在 `wechat.ocr.group_names` 中覆盖。

## 部署（Windows）

1. 官方 PC 微信已登录，窗口可见，不要锁屏，不要断开远程桌面会话。使用可丢的专用小号。
2. 安装项目依赖后，按所选引擎额外安装（安装 ≠ 已验证可用）：
   - 驱动：`pip install pyautogui pillow`
   - Tesseract 引擎：系统安装 Tesseract，再 `pip install pytesseract`
   - 或 RapidOCR：`pip install rapidocr-onnxruntime`
3. 复制配置，先保持 `dry_run = true`。
4. 跑自检（默认 dry-run，不会点击真实窗口）：

```powershell
.\.venv\Scripts\python.exe scripts\selfcheck_ocr.py
```

5. `WindowsScreenDriver` / `TesseractOcrEngine` / `RapidOcrEngine` **尚未在真实微信 4.x 上验证**。在 Linux 上 import 本模块不会报错；实例化 Windows 驱动会明确提示平台或缺依赖。不要在未验证时把 `dry_run` 关掉对着真实会话试错。

## 风险自报

- 协议 / 接入方式：对已登录的官方 Windows 客户端做屏幕截图与键鼠操作（RPA / OCR），不是 Hook、不是协议登录、不是网页版。
- 是否容易被官方检测：中等。风险主要来自**发送时的点击与粘贴模式**，而不是截图本身。仍可能违反个人微信自动化相关条款。用户已接受专用小号的封号风险，但不能把 OCR 当成低风险通道。
- 延迟：常达十余秒（截图 + OCR + 输入停顿 + 拟人延迟）。不满足「秒回」，也不应为此关掉拟人延迟。
- 准确性：找不到群名则拒绝发送；找错输入框或按钮可能误操作其它窗口——这是降级方案固有风险，所以默认 dry-run，并要求先确认群名。
- 验证码：只暂停、不绕过。把验证码识别或自动提交做成功能是明确禁止的。
- 验证状态：真实驱动与引擎为未验证骨架。box 只跑 Fake。需要用户在 Windows 上验证：能否定位指定群的输入框和发送按钮、dry-run 是否只截图不点击、创建 `data/PAUSE` 能否立即停止、弹出验证码时是否写入暂停文件。

严禁把本适配器用于博彩、结算、收款或任何被禁止的功能。

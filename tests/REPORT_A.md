# 适配器 A（本地库只读抓取）核查报告

- 核查时间：2026-11-11 03:50（Asia/Shanghai，UTC+8）
- 仓库：`/workspace/wechat-community-bot`，分支 `master`
- 提交：`8d4c9ce`、`a384e21`、`66332a8`、`5647071`
- 范围：`docs/ADAPTER_A.md`、`src/adapters/local_db.py`、`src/adapters/local_capture.py`、契约 AD-01、官方用例与自检、补充用例 `tests/review_a/`
- 约束遵守：未改 `src/`、未 push、未使用 Codex、未建轮询例程、未修改已有测试文件、未删除或改动仓库 `data/PAUSE`（本轮自始至终不存在该文件；暂停相关用例均用 `tmp_path` + `WECHAT_BOT_PAUSE_FILE` 隔离）

## 结论

**不通过。** 请开发修复后退回复测。

官方交付用例与模拟自检全部通过，核心增量抓取、乱序/去重、免打扰与折叠群、重启续读、库锁重试、对账补抓、只读 URI、observe/PAUSE 协同在模拟库上行为正确，也未发现博彩/结算/收款或 Hook/协议实现。但补充核查发现两类必须先修的问题：

1. 游标 JSON 损坏时会静默从 0 重放全部已处理消息（高，破坏「重启不重复」）。
2. `assert_readonly()` 在连接实际可写时也可能因为缺列/非空约束把写入失败误判为「只读」（高，只读探测不可信）。

另外，日志脱敏覆盖不到 `WECHAT_LOCAL_DB_KEY=` 以及配置字段 `key` / `db_key`，宽权限密钥文件无告警，`seen_ids` 无窗口裁剪会随运行时间膨胀。这些作为退回修复项一并列出。

## 官方用例与自检证据

执行环境：仓库 `.venv`，Python 3.13.5，pytest 9.2.0。工作目录为仓库根目录。`data/PAUSE` 运行前后均不存在。

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 适配器 A 单元 | `pytest tests/test_adapter_local_capture.py -v` | **14 passed** |
| 全量抓取对账 CAP-00～CAP-06 | `pytest tests/test_capture.py -v` | **7 passed** |
| 契约 AD-01（7 条） | `pytest tests/test_adapter_contracts.py -k ad01 -v` | **7 passed** |
| 以上合计 | 一次跑完 28 selected | **28 passed**，约 43.8s |
| 自检脚本 | `python scripts/selfcheck_local_capture.py --simulate` | **通过**（首次 6 条，重启后续读 1 条，免打扰/折叠/未打开群已覆盖） |

对照 `docs/ADAPTER_A.md` 与 `tests/contracts/local_db_reader.py`、`tests/README.md`：已实现 `LocalDbReader.poll` / `iter_new` / `get_cursor` / `set_cursor` / `fetch_by_ids` / `assert_readonly`，以 `mode=ro` URI 打开，游标按 `create_seq` 而非纯时间戳，白名单抓取不看会话是否打开。与文档及 AD-01 契约对齐。文档未把「2 秒内」写成硬指标，轮询默认 1s±0.1s，事件带 `detected_at`，符合现行验收口径。

开发自报属实：不内置解密/密钥提取；`get_key()` 仅在提供器中定义，读取路径从未调用，打开的是用户提供的已解密/明文库；`seen_ids` 确无窗口裁剪；真实 4.x 表结构仍须 Windows 验证。

## 补充核查证据（`tests/review_a/`）

`pytest tests/review_a -v`：**41 passed，5 failed**（约 3.1s）。失败项均为产品缺陷，不是测试误伤。

通过的重点包括：

- 只读 URI 含 `mode=ro`；经该 URI 的完整 INSERT 失败；DELETE 日志库 poll 后主库 sha256 / size / mtime 不变，且未创建 `-wal`/`-shm` 以外的旁路文件；WAL 库只读打开后主库哈希不变。
- 乱序时间戳、同时戳多条、重复行、并发 80 条、突发 500 条/分钟对账漏抓率=0、重复处理率=0。
- 库锁后补拉不漏不重；库损坏与缺表抛出 `LocalDbCorruptError`，不会当成空结果。
- 文件游标重启续读、库文件替换靠 `seen_ids` 去重、免打扰/折叠/未打开群不漏。
- 对账缺口可用 `catch_up` 补回且第二次为空；时钟回拨后仍按入库序抓到新消息；naive / `+08:00` / `Z` / unix 时间戳可解析。
- `PAUSE` 存在时适配器 A 仍抓取（暂停只约束发送）；`observe` 模式下 `SendGate` 丢弃发送，outbound 记录为空。
- 禁止功能标识符与适配器 A 关键词扫描通过；`requirements.txt` 仅 `pytest`、`pyyaml`，无 frida / sqlcipher / wxdump 等；A 的实现只依赖标准库 + 项目内模块。

5 个失败用例见下一节，作为退回依据。

## 具体问题

### P1. 游标文件损坏时静默归零并重放全部消息（高）

- 位置：`src/adapters/local_db.py:518-525`（`CursorStore._load_file`），日志在 524 行。
- 现象：主游标文件被截成半段 JSON 后，加载逻辑只打 warning「游标文件无法读取，将重新开始」，然后 `last_seq=0`、`seen_ids=[]`。随后 `poll()` 把已经处理过的消息全部再吐一遍。
- 证据：`tests/review_a/test_cursor_reconcile.py::test_corrupt_cursor_file_must_not_silently_replay` 失败，重放 `cur-0000`…`cur-0005`。
- 说明：正常路径的「先写 `.tmp` 再 `replace`」是原子的，残留损坏 `.tmp` 不会破坏完整主文件（该补充用例已通过）。但主文件一旦损坏（磁盘问题、人工编辑、极少数文件系统上的替换中断），当前实现会破坏「重启不重复」。应失败关闭：保留/回退 `.bak`，或直接抛错停止抓取，禁止静默从 0 开始。

### P2. `assert_readonly()` 可能把可写连接误判为只读（高）

- 位置：`src/adapters/local_db.py:473-481`（`SqliteDbReader.assert_readonly`）。
- 现象：探测写入只执行 `INSERT INTO messages(msg_id) VALUES ('probe')`。模拟库（以及多数真实表）有多个 `NOT NULL` 列，可写连接上这条 INSERT 也会因约束失败。方法把任意 `sqlite3.Error` 都当成「只读成功」。
- 证据：`tests/review_a/test_readonly_guarantee.py::test_assert_readonly_detects_writable_connection` 把 `_connect` 换成普通可写连接后，**没有**抛出 `AssertionError`。
- 说明：生产路径确实使用 `mode=ro`，真正只读打开时写入会失败，这一点有其他用例证明。但自检与契约所依赖的 `assert_readonly()` 本身不可信，且成功/失败后都未关闭连接（见 P8）。应改成不依赖表结构的只读判定（例如确认错误信息含 `readonly`，或打开后执行 `PRAGMA query_only=ON` 并在 `finally` 里关闭）。

### P3. 日志脱敏覆盖不到 `WECHAT_LOCAL_DB_KEY=...`（中高）

- 位置：`src/logging_util.py:16-25、49-69`（`redact`）。
- 现象：`WECHAT_LOCAL_DB_KEY=super-secret-...` 这类赋值不会被掩码，因为正则只匹配 `api_key` / `token` / `secret` 等，`DB_KEY` 中的 `KEY` 与前面的 `_` 连在一起，构不成独立词界。
- 证据：`tests/review_a/test_secrets_and_redact.py::test_redact_masks_wechat_local_db_key_assignment` 失败，原文完整保留。
- 说明：适配器 A 多数日志已手动 `redact(str(exc))`，且 `get_key()` 不会把密钥写进异常。但只要密钥或环境变量赋值进入日志或异常链，现有脱敏挡不住。应把 `WECHAT_LOCAL_DB_KEY`、`db_key`、`local_db_key` 纳入规则。

### P4. `mask_config` 不遮蔽 `key` / `db_key` / `WECHAT_LOCAL_DB_KEY`（中）

- 位置：`src/logging_util.py:27-31、72-84`（`_SENSITIVE_KEY_RE` / `mask_config`）。
- 现象：示例与文档虽然要求密钥不要写入配置，但若有人把密钥放进 `key`、`db_key` 或同名环境变量字段，`safe_config_text` / `mask_config` 仍会原样打出。
- 证据：`tests/review_a/test_secrets_and_redact.py::test_mask_config_hides_key_fields` 失败。

### P5. 密钥文件对他人口可读时既不拒绝也不告警（中）

- 位置：`src/adapters/local_db.py:123-138`（`EnvFileKeyProvider.get_key`）。文档（`docs/ADAPTER_A.md` 与错误文案）写明「文件权限应仅限当前用户」，实现未检查。
- 证据：`tests/review_a/test_secrets_and_redact.py::test_world_readable_key_file_should_be_rejected_or_warned` 失败（`0644` 文件仍直接返回密钥，无日志）。
- 建议：权限含组/他人读时拒绝，或至少 warning。

### P6. `seen_ids` 无窗口裁剪，游标文件与内存会线性膨胀（中，开发已自报）

- 位置：`src/adapters/local_db.py:537-558`（每次完整重写 JSON）、`636-637`、`790`、`663-664`。每轮 `poll` 批次结束后都会把全部 `seen_ids` 落盘，无 `prune` 接口。
- 实测（`test_seen_ids_unbounded_growth_estimate`，indent=2 的 JSON）：约 **33.1 字节/条**；2000 条样本文件 66129 字节。
- 长跑估算（按交易会员群量级，仅适配器 A 处理的白名单消息）：

  | 日增量 | 30 天 | 1 年 | 到约 10 MiB 所需时间 |
  | --- | --- | --- | --- |
  | 800 条/天 | ≈ 0.76 MiB | ≈ 9.2 MiB | 约 1.1 年 |
  | 2000 条/天 | ≈ 1.9 MiB | ≈ 23.0 MiB | 约 158 天 |
  | 8000 条/天 | ≈ 7.6 MiB | ≈ 92.1 MiB | 约 40 天 |

  内存中还有一份 `set[str]`；且每秒级轮询都会重写整个游标文件，文件越大 IO 越重。
- 建议裁剪策略：
  1. 正常运行以 `last_seq` 为权威游标，`seen_ids` 只保留「最近窗口」（例如最近 2 万条，或 `last_seq` 往前 1 万序号 / 7 天）用于去重同时戳与重复行。
  2. 检测到 `file_identity` 变化（库被替换、微信重启后 seq 回退）时，不要丢弃历史 ID：可把旧 `seen_ids` 压缩存到旁路 SQLite，或在替换后的短窗口保留完整集合，稳定后再剪。
  3. 游标落盘改为 SQLite/追加日志，避免每轮重写整份 JSON。
  4. 定期对账补抓，防止窗口外又出现「seq 已过但未入 seen」的缺口。

### P7. `last_seq` 与 `seen_ids` 不一致时，增量 `poll` 不会回看缺口（中）

- 位置：`src/adapters/local_db.py:400-404`（`fetch_after` 只取 `seq > last_seq`）；`src/adapters/local_capture.py:323-326`（后台循环只 `poll_once`，**没有定期 `reconcile`/`catch_up`**）。
- 现象：若 `last_seq` 已经推到 5，但 `seen_ids` 缺了中间一条（崩溃窗口、人工改游标、将来裁剪不当），`poll()` 返回空；`catch_up` 仍能按 ID 补回。补充用例 `test_poll_misses_gap_when_last_seq_ahead_of_seen_ids` 记录了这一边界（用例本身通过，但是产品风险）。
- 建议：启动或每隔 N 轮按时间窗对账；或在发现 `last_seq` 与 `seen_ids` 明显不一致时回退一个安全窗口再扫。

### P8. 其他中低项（不单独阻断，建议一并改）

1. **`assert_readonly` 泄漏连接**（中低）：`local_db.py:473-481` 经 `_ensure()` 打开后，无论写入失败还是误成功，都不 `close()`。实测 `_conn` 会一直保持到显式 `close()` / 下一次 `_execute`。只读连接通常不阻塞 WAL 写端，但长时间占用仍应避免。请在 `finally` 中关闭。
2. **缺表报错文案绕过专门检查**（低）：`_detect_restart()` 先调 `max_seq()`（`local_db.py:417-422、673-680`），缺表时得到 `LocalDbCorruptError: 本地库无法读取: no such table: messages`，而不是预定的「消息表不存在，请检查 schema 映射」。类型正确，不会当成空结果，但自检提示不够友好。应先做表存在检查。
3. **无法解析的时间戳回退到 `datetime.now()`**（低）：`parse_timestamp`（`local_db.py:566-590`）在坏值时忽略注入时钟，对账时间窗可能错位。宜记明确错误或使用注入时钟。
4. **单次 `poll()` 最多连续跳过 4 批被过滤行**（低，`local_db.py:773-804`）：若游标后堆积超过约 800 条非白名单/已见行，本轮可能先返回空，下一轮继续。后台 1 秒轮询最终能追上，但一次性拉取时要注意。可去掉 `idle_rounds` 上限或改成「直到本轮没有任何进度」。
5. **密钥提供器在 MVP 中未被用于打开库**（信息，符合声明）：即使 `require_key=true` 且已配置密钥，也只做「是否配置」检查，不会拿密钥去打开 SQLCipher/WCDB。真实加密库若误配成 `db_path`，会表现为损坏/无法识别。须在 Windows 上用「已解密副本」验证，并在自检中写得更直白。

## 退回修复项

请开发在不引入 Hook/协议、不写原库的前提下修复后通知复测：

1. 【高】游标主文件损坏时不得静默归零重放：保留 `.bak` 并回退，或抛明确错误并停止抓取。补回归用例（可参考 `tests/review_a/test_cursor_reconcile.py::test_corrupt_cursor_file_must_not_silently_replay`）。
2. 【高】重写 `assert_readonly()`：探测不得依赖「只插 `msg_id`」的约束失败；必须能区分只读错误与 schema 错误；`finally` 关闭连接。
3. 【中高】扩展 `redact` / `mask_config`，覆盖 `WECHAT_LOCAL_DB_KEY`、`db_key`、`key` 等字段与赋值形式。
4. 【中】密钥文件权限过宽时拒绝或告警。
5. 【中】为 `seen_ids` 实现窗口裁剪 + 库文件替换时的保留策略，并避免每轮重写超大 JSON；评估后给出默认窗口（建议至少覆盖数日或 2 万条量级）。
6. 【中】在运行路径增加定期对账补抓，或在 `last_seq` 与 `seen_ids` 不一致时回扫安全窗口，避免缺口只能靠人工调用 `catch_up`。
7. 【低，建议同批】缺表先做映射检查再 `max_seq`；坏时间戳不要静默改写成墙钟；评估 `idle_rounds` 上限。

未发现博彩/六合彩/结算/收款功能，也没有 Hook/协议实现，这两项无需退回。示例配置与 `.gitignore`（`*.key`、`config/local.*`、`data/`）对密钥落盘的约定是合格的。

## 必须在用户 Windows 真机验证的项

Linux box 只能用模拟库。下列事项在登录官方 PC 微信 4.x、使用可丢专用小号与家庭网络的机器上确认之前，不能把适配器 A 视为可上线观察：

1. `db_path` 指向的已解密/明文库能否用 `mode=ro` 打开；官方客户端持有写锁时，只读打开是否被长时间挡住，busy 重试是否够用。
2. 当前 4.x 小版本的表名/列名、群 ID 形态（是否 `xxx@chatroom`）、入库序、消息类型取值是否与 `schema` 映射一致。必须先跑 `scripts/selfcheck_local_capture.py --config config/local.toml`。
3. 时间戳是 ISO、秒/毫秒 unix，还是 Windows FILETIME（约 1e17 量级）。当前解析只把 `>1e12` 当作毫秒，FILETIME 会错。
4. 免打扰、折叠、当前未打开的交易会员群，只要客户端已同步，文本与非文本占位是否都能被增量检出（记录 `detected_at` 与消息时间的分位数，不作「2 秒内」硬性承诺）。
5. 微信重启、库文件被替换或短暂锁定后，游标能否续读且不漏不重；并人为验证「游标文件损坏」修复后的行为。
6. 自己在群里发的消息是否带 `is_self`；图片/语音等是否留下类型占位。
7. 若客户端库是 WAL 模式：只读打开是否改写主库或长时间占用 `-shm`，是否影响微信自身写入。
8. 确认打开的是「已解密副本或明文库」。不要指望本适配器使用环境变量里的密钥做解密；加密库应表现为明确失败，而不是空轮询。
9. 观察 7–14 天：账号是否出现强制下线、验证码、「环境异常」或禁止登录电脑。出现则立刻停止，不要自动反复重登。不承诺不封号。
10. 按真实群日增量观察 `data/local_capture_cursor.json` 体积（在裁剪修复上线前尤其必要）。

## 安全与协同摘要

- 禁止功能扫描（适配器 A 源码与文档、产品 `src/` / `config/` 标识符）通过；文档中的「严禁博彩/结算/收款」「不 Hook」属于禁止声明，未当作实现。
- 依赖与权限：无额外提权清单；文档建议不要用管理员权限运行。
- `data/PAUSE`：适配器 A 不检查该文件，暂停期间仍只读抓取，符合「PAUSE 是发送闸门」的约定。`observe` 模式下发送出口 `SendGate` 会丢弃自动回复与公告，A 只负责采集。协同行为正确。
- 本仓库示例配置未写入密钥明文。

## 复测说明

开发按退回项修复后，请再通知核查。复测将重跑：

- `pytest tests/test_adapter_local_capture.py tests/test_capture.py tests/test_adapter_contracts.py -k "ad01 or local_capture or local_db or cap0"`
- `python scripts/selfcheck_local_capture.py --simulate`
- `pytest tests/review_a`

通过标准：上述官方用例与自检保持全绿；补充用例中 P1–P5 对应失败项转为通过；P6/P7 至少有可验证的裁剪或定期对账方案。Windows 真机项仍须用户侧验证，不在本次 box 复测范围内。

## 复测（2026-11-11 04:14，Asia/Shanghai，UTC+8）

- 仓库：`/workspace/wechat-community-bot`，HEAD `f570b08`
- 提交范围：`57841b2`、`8e36c98`、`928881f`、`f570b08`
- 约束遵守：未改 `src/`、未 push、未使用 Codex、未建轮询、未修改已有测试文件；新增用例在 `tests/review_a/test_retest_fixes.py`；未删除仓库 `data/PAUSE`（本轮自始至终不存在该文件；用例均用 `tmp_path` 隔离）
- 解释器：仓库 `.venv`，Python 3.13.5，pytest 9.2.0，`BOT_TEST_USE_MOCK=0`（real 模式）

### 结论

**通过。** 首轮退回的 7 项均已修复并可验证。未发现博彩 / 结算 / 收款或 Hook / 协议实现。禁止功能关键词与脱敏扫描（`tests/review_a` 原有安全用例）全部通过。

有两处残余，不阻断本轮通过，建议开发一并处理：

1. **已被 `trim_seen_window` 裁掉的 ID，库文件替换后会当作新消息重放。** 替换时仍留在去重集里的 ID 会被 `freeze_trim` 保住（默认窗口 2 万条 / 1 万序号足够覆盖数日）。但激进窗口下已裁掉的 oldest ID 会漏出，见 `test_already_trimmed_ids_may_replay_after_file_replace`（重放 `old-1`）。位置：`src/adapters/local_db.py:841-878`、`915-916`。
2. **官方自检脚本只删除 `cursor.json`，不删 `.bak`。** 主文件缺失时实现会回退备份（崩溃恢复是对的），因此第二次跑 `scripts/selfcheck_local_capture.py --simulate` 会把旧备份当成游标，对账全漏（本轮首次复跑即因此失败，miss 全部 `sim-*`）。把残留 `.bak` 挪开后自检通过：首次 6 条，重启续读 1 条。建议脚本同时清理 `.bak`；若用户只删主游标想重置，也会被备份拉回去。位置：`scripts/selfcheck_local_capture.py:172-173`；回退逻辑 `src/adapters/local_db.py:618-635`。

Windows 真机项仍须用户侧验证，不在本次 box 复测范围内。

### 逐条对照

| 退回项 | 结论 | 证据 |
| --- | --- | --- |
| 1. 游标 JSON 损坏→回退备份 / 无备份停止，绝不归零重放 | **通过** | 有 `.bak` 时损坏主文件后 `poll()` 为空，损坏文件被改名为 `cursor.json.corrupt-*`，随后新消息可增量抓取。无备份时构造 `LocalDbReader` 抛 `CursorCorruptError`，不会从 0 重放。官方 `test_corrupt_cursor_falls_back_to_backup_without_replay`、`test_corrupt_cursor_without_backup_raises` 与补充 `tests/review_a/test_retest_fixes.py` 对应用例均通过。首轮失败的 `test_corrupt_cursor_file_must_not_silently_replay` 现已通过。 |
| 2. `assert_readonly` 改 CREATE TABLE 探测 | **通过** | 可写连接（monkeypatch `_connect`）抛 `AssertionError`（含「只读」），探测表 `_local_capture_ro_probe` 已回滚，库哈希不变，`_conn` 为 `None`。默认只读 URI 探测通过且不改库。官方 `test_assert_readonly_rejects_writable_and_closes` 与首轮 `test_assert_readonly_detects_writable_connection` 均通过。实现：`src/adapters/local_db.py:509-544`。 |
| 3. `redact` 覆盖 `WECHAT_LOCAL_DB_KEY=` | **通过** | `WECHAT_LOCAL_DB_KEY=super-secret-...` 被掩码，密钥明文不再出现。首轮失败用例现已通过。 |
| 4. `mask_config` 遮蔽 `key` / `db_key` / `WECHAT_LOCAL_DB_KEY` | **通过** | 三字段值均为 `***`。首轮失败用例现已通过。 |
| 5. 密钥文件 0644 告警 / strict 拒绝 | **通过** | 非 strict 打出「权限过宽」告警并仍可读；`strict=True` 或 `WECHAT_LOCAL_DB_KEY_STRICT=1` 抛 `KeyPermissionError`。首轮失败用例现已通过。实现：`src/adapters/local_db.py:154-172`。 |
| 6. `seen_ids` 裁剪 `trim_seen_window` | **通过**（有残余，见上） | 窗口 15/10 时裁剪后 `seen_ids<=15`，近期消息不重复处理，新消息可抓。无新消息时闲置 `poll` 不重写游标文件（哈希与 mtime 不变）。库替换且 ID 仍在集内时只出新消息。官方 `test_trim_seen_window_caps_memory` 通过。默认 `seen_ids_max=20000`、`seen_seq_window=10000`。 |
| 7. `last_seq` 超前且 seen 缺号→`reconcile_recent` 每 60s 对账，`poll` 增量不变 | **通过** | `poll()` 在缺口下仍返回空（增量语义不变，首轮边界用例仍通过）；`reconcile_recent` 补回 `gap-0002` 且第二次为空。`LocalCaptureAdapter` 默认间隔 `DEFAULT_RECONCILE_INTERVAL=60.0`：首次 `poll_once` 会对账，之后 59s 内不补，满 60s 补回缺口。官方 `test_reconcile_recent_fills_gap_left_by_poll` 通过。实现：`src/adapters/local_db.py:1064-1088`，`src/adapters/local_capture.py:48,317-358`。 |

### 回归

乱序 / 同时戳 / 重复行 / 并发写 / 突发 500 条每分钟 / 库锁 / 重启续读 / 免打扰、折叠、未打开群：漏抓与重复处理均为 0。只读前后库文件 sha256 不变。上述场景由首轮 `tests/review_a` 用例与复测 `test_regression_hash_unchanged_and_zero_miss_dup` 覆盖，全部通过。

### 命令与计数

| 项 | 结果 |
| --- | --- |
| `pytest tests/test_adapter_local_capture.py tests/test_capture.py tests/test_adapter_contracts.py -k "ad01 or ad00 or local_capture or capture or local_db"` | **34 passed** |
| `python scripts/selfcheck_local_capture.py --simulate` | 工作目录留有上次 `.bak` 时 **未通过**（全量漏抓）；挪开残留备份后 **通过**（首次 6 条，重启续读 1 条） |
| `pytest tests/review_a`（含首轮补充 + 本轮复测） | **59 passed** |
| 全量 `BOT_TEST_USE_MOCK=0 pytest tests`（real） | **902 passed，3 failed，1 skipped**（约 50s）。失败 3 项：`tests/review_b/test_b_retest_gates_and_driver_boundary.py` 两条（UIA 标题全半角规范化、粘贴异常未收口，属 B，不在本次范围）；`tests/review_cd/test_retest_fixes.py::test_direct_send_text_must_not_bypass_run_mode`（属 D，见 `REPORT_CD.md`）。跳过 1 项：`tests/test_security.py:206` pip audit 不可用。 **A 范围内无新失败。** 开发预告的 B/E 旧失败（review_b 绕过闸门、review_e latency/run_mode、`test_adapter_uia` no_retry）本轮全量中未再出现。 |

开发模块 E / B 仍可能并行修改中，本结论只覆盖适配器 A 的修复与回归。

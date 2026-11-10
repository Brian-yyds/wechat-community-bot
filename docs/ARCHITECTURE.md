# 架构说明

本仓库是「交易会员群」微信机器人的第一阶段骨架。功能仅限：实时监控群消息、消息整理识别、自动回复、定时公告。严禁博彩 / 六合彩 / 下注 / 结算 / 收款相关功能。

第一阶段不绑定任何真实微信协议，所有收发都走适配层。后续接入真实协议时，只需新增一个 `Adapter` 实现，不必改动管线、调度器与限速模块。

## 模块关系

```
真实微信协议 / MockAdapter
        │  Event（Message）
        ▼
   消息处理管线 Pipeline
   去重 → 要点提取 → 分类 → 规则回复
        │                │
        ▼                ▼
   JSONL 落盘      HumanizedSender（优先级队列）
                            ▲
   定时公告调度 ─────────────┘
                            │
                     限速令牌桶 + 随机延迟
                     + 夜间静默 + 正在输入钩子
                            │
                            ▼
                      Adapter.send_text()
```

时钟（`src/clock.py`）可注入，便于测试与演示拨快时间。

## 适配器接口（稳定契约）

实现位于 `src/adapters/base.py`。后续接入真实协议时请保持下列公开签名不变。

### 数据类

```python
class EventType(str, Enum):
    MESSAGE = "message"
    ADAPTER_STARTED = "adapter_started"
    ADAPTER_STOPPED = "adapter_stopped"

@dataclass(frozen=True)
class Group:
    group_id: str
    name: str
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Message:
    message_id: str
    group_id: str
    sender_id: str
    content: str
    timestamp: datetime
    sender_name: str = ""
    mentions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Event:
    event_type: EventType
    timestamp: datetime
    message: Message | None = None
    extra: dict[str, Any] = field(default_factory=dict)
```

### Adapter 方法

```python
class Adapter(ABC):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def send_text(self, group_id: str, text: str) -> None: ...
    def list_groups(self) -> list[Group]: ...
    def subscribe(self, callback: EventCallback) -> Callable[[], None]: ...
    def event_queue(self) -> queue.Queue[Event]: ...
    def async_event_queue(self) -> asyncio.Queue[Event]: ...
    def notify_typing(self, group_id: str) -> None: ...  # 可选，默认空实现
```

- `subscribe` 为推送式，可注册多个回调，返回取消函数。
- `event_queue` 为线程安全的拉取式队列。
- `async_event_queue` 为异步拉取式队列，真实协议实现可覆盖。
- `notify_typing` 供拟人发送器模拟「正在输入」。

本地测试使用 `MockAdapter`：可从脚本、JSON/JSONL 文件或标准输入注入消息，并记录 `sent_messages` 与 `typing_events`。

读取与发送可以分离：`InboundAdapter` 只收消息，`OutboundAdapter` 只发送，`CompositeAdapter` / `build_adapter()` 按配置组合。
为后续四种适配器预留了角色名（尚未实现）：A `local_capture` 只读抓取、B `uia` 发送、C `notification` 通知预览唤醒、D `ocr` 降级发送。配置项为 `wechat.inbound` 与 `wechat.outbound`。

## 其他模块

- `src/pipeline/`：去重（消息 ID + 同群内容哈希 + 时间窗，状态可持久化到 `data/`，重启不重复回复）、守卫（不回自己、不对他人机器人回复、突发熔断）、`ReplyProvider` 抽象（默认规则/问答库，可选 LLM 只留接口）、规则分类（提问 / 行情讨论 / 广告刷屏 / 公告回复 / 闲聊/无关 / 需人工；禁止类为「禁止功能-上报」并带 forbidden_report）、要点提取、可配置回复（关键词 / 正则 / 问答库，含冷却、群白名单、优先级），结果以 JSONL 写入 `data/`。
- `src/scheduler/`：配置驱动的定时公告。支持 `HH:MM` 每日定点或 5 字段 cron；在计划时刻附近附加 `[−jitter_seconds, +jitter_seconds]` 的稳定抖动（默认 ≤3 秒，须落在准点 ±5 秒内）；错过补发可配置，默认超过 `catch_up_max_minutes` 不补发。
- `src/ratelimit/`：全局 / 单群 / 发送类型（公告与自动回复）独立令牌桶；随机发送延迟；夜间静默（公告默认暂存，自动回复默认丢弃）；优先级队列。
- `src/logging_util.py`：日志脱敏（手机号、wxid、邮箱、token/key）。`src/config_util.py` 加载配置时只打印脱敏后的内容。
- `src/safety.py`：加载回复规则与公告文案时扫描违禁词，命中则拒绝启动。

## 配置

- 示例：`config/example.toml`（同时提供 `.json` / `.yaml`）。
- 本地覆盖：`config/local.toml`（或 `.json` / `.yaml`），已加入 `.gitignore`。
- 也可用环境变量 `WECHAT_BOT_CONFIG`、`WECHAT_BOT_DATA_DIR`。
- 不要把密钥写入仓库。

## 接入真实协议的建议

1. 新建 `src/adapters/xxx.py`，实现 `Adapter` 的全部抽象方法。
2. 把协议事件映射为 `Message` / `Event`，保留原始字段到 `extra`。
3. 在 `src/main.py` 按配置选择适配器，其余模块无需改动。
4. 发送路径必须继续走 `HumanizedSender`，不要绕过限速直接 `send_text`。

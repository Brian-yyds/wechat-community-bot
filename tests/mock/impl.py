# -*- coding: utf-8 -*-
"""简易 mock 实现，仅用于跑通 tests/ 脚手架逻辑，验证用例自洽。

位置：tests/mock/ —— 请勿把本目录当作产品代码引用或复制到 src/。
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time, timezone
from typing import Callable, Dict, List, Optional

from constants import (
    CATCH_UP_WINDOW_DEFAULT,
    CATEGORIES,
    FORBIDDEN_CATEGORY,
    FORBIDDEN_FEATURE_KEYWORDS,
    GROUP_HOURLY_LIMIT_DEFAULT,
    HOURLY_LIMIT_DEFAULT,
    HUMANIZE_DEFAULT,
    NIGHT_SILENCE,
    TYPING_CHARS_PER_SECOND,
    TYPING_DELAY_MAX,
)

# 复用分类标签，便于测试 CL-01
CATEGORIES = list(CATEGORIES)  # noqa: F811

SHANGHAI = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------

_FORBIDDEN_RE = re.compile(
    "|".join(
        re.escape(k)
        for k in FORBIDDEN_FEATURE_KEYWORDS
        + ["收款账户", "收款", "结算", "赌局", "代付", "开一注", "六合彩"]
    ),
    re.I,
)

_AD_RE = re.compile(
    r"("
    r"加\s*微\s*信|加我微信|加\S{0,6}微信|加薇|加\s*v\s*信|加\s*v\s*[:：]|jia\s*wei\s*xin|"
    r"加我ｖｘ|加我vx|加我vx|ｖｘ|加\s*ｖｘ|"
    r"带单|拉人头|拉人进|每拉一位|每拉五人|"
    r"https?://|www\."
    r"|私聊(我|领|询|报名|安排)|想要的私聊|详情私聊|"
    r"稳赚|日入过万|招代理|返佣|"
    r"内部名额|跟单|信号群|信号每天|信号社|免费信号|老师带单|"
    r"机器人出租|保本跟单|不是广告|代理招商|直播带单|"
    r"私聊领福利|先加我微信|加我，带你|同步下单|"
    r"加\s*v\s*[:：]|加薇\s*xin"
    r")",
    re.I,
)

_HUMAN_RE = re.compile(
    r"(被盗|被骗|验证码|纠纷|投诉|请管理员|管理员介入|冒充管理员|"
    r"注销账号|人工核对|人工复核|人工审核|需要人工|请人工|请求人工|"
    r"无法登录|风控人工)"
)

_ADVICE_ASK_RE = re.compile(
    r"(该不该买|要不要止损|给我一个方向|直接给结论|现在能买|现在该买|"
    r"帮我看看这笔合约|该不该买入)"
)

_QUESTION_RE = re.compile(
    r"(怎么|如何|请问|怎么办|是多少|入口在哪|生效吗|几点结束|"
    r"要多久|要不要\s*KYC|是不是|有没有|从哪看|怎么算|怎么做|"
    r"分别是多少|能不能导出|有什么差别|是什么意思|哪一步|在哪看|"
    r"要填什么|最低额度|为什么|吗[？?]?|？|\?)"
)

_QUESTION_TOPIC_RE = re.compile(
    r"(开户|提币|提现|手续费|fee|withdraw|爆仓|强平|充值|杠杆|"
    r"密码|API|api|白名单|费率|维护|穿仓|官方公告|官方|"
    r"KYC|kyc|maker|taker|gas|流水|发票|风险测评|交易权限|"
    r"新费率|记算|计算)"
)

_ANNOUNCE_ACK_RE = re.compile(
    r"^(收到公告|收到，|收到$|已阅|已读|明白了|好的收到|好的，|嗯嗯|"
    r"收到，谢谢|收到，新人|收到，费率|收到，置顶|置顶看过了|"
    r"维护期间先不挂单)"
)

_MARKET_RE = re.compile(
    r"(BTC|ETH|btc|eth|meme|土狗|看涨|看跌|走势|大盘|"
    r"减半|dominance|pump|dump|支撑位|前高|阴跌|山寨|"
    r"标记价格|四小时|三角形|链上活跃|现货囤着|抄还是等|"
    r"跌破|拉回来|空了一小仓|止损|成交量|散户|以太坊)",
    re.I,
)

_FW_TRANS = str.maketrans(
    {
        "ｖ": "v",
        "Ｖ": "v",
        "ｘ": "x",
        "Ｘ": "x",
        "：": ":",
        "　": " ",
    }
)


def _norm(text: str) -> str:
    return (text or "").translate(_FW_TRANS).strip()


def classify(message: dict) -> dict:
    text = _norm(message.get("text") or "")
    mentions = message.get("mentions") or []
    quoted = ""
    if isinstance(message.get("quote"), dict):
        quoted = _norm(message["quote"].get("text") or "")
    blob = text + "\n" + quoted

    if not text:
        return _out("闲聊/无关", False, "", [])

    if _FORBIDDEN_RE.search(blob):
        return _out(FORBIDDEN_CATEGORY, False, "", ["forbidden_report"])

    if _HUMAN_RE.search(blob) or (
        ("交易助手" in mentions or "@交易助手" in text) and _ADVICE_ASK_RE.search(blob)
    ):
        return _out("需人工", False, "", ["need_human"])

    if _AD_RE.search(blob):
        return _out("广告刷屏", False, "", ["spam"])

    # 纯闲聊里出现「不聊行情」不应进入行情类
    market_hit = bool(_MARKET_RE.search(blob))
    if re.search(r"(不聊行情|别聊行情)", blob):
        market_hit = bool(_MARKET_RE.search(re.sub(r"(不|别)聊行情", " ", blob)))

    is_q = bool(_QUESTION_RE.search(blob))
    has_topic = bool(_QUESTION_TOPIC_RE.search(blob))
    # 链上手续费/币种行情里的「手续费」优先作为行情讨论，而不是客服提问
    market_overrides_fee_q = market_hit and re.search(
        r"(以太坊|ETH|BTC|链上活跃|这波手续费)", blob, re.I
    ) and not re.search(r"(开户|提币|提现|爆仓|强平|平台手续费|现货手续费|合约)", blob)

    # 公告跟评里提出具体问题 -> 提问
    if is_q and has_topic and not market_overrides_fee_q:
        return _out(
            "提问",
            True,
            "请查阅群置顶的官方说明或联系管理员。我只能提供流程信息，不提供买卖建议，也不承诺任何收益。",
            [],
        )

    # 纯公告确认
    compact = re.sub(r"\s+", "", text)
    if _ANNOUNCE_ACK_RE.search(text) or _ANNOUNCE_ACK_RE.search(compact):
        return _out("公告回复", False, "", [])

    if market_hit:
        return _out("行情讨论", False, "", [])

    # 「开户怎么搞」无问号也可被 topic+怎么 捕获；再兜底一次
    if has_topic and re.search(r"(怎么|如何|请问|入口|多少|怎么办)", blob):
        return _out(
            "提问",
            True,
            "请查阅群置顶的官方说明或联系管理员。我只能提供流程信息，不提供买卖建议，也不承诺任何收益。",
            [],
        )

    return _out("闲聊/无关", False, "", [])


def _out(category, should_reply, reply, flags):
    return {
        "category": category,
        "should_reply": should_reply,
        "reply": reply if should_reply else "",
        "flags": list(flags),
    }


# ---------------------------------------------------------------------------
# 时钟 / 调度
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, now: Optional[datetime] = None):
        self._now = now or datetime(2026, 11, 11, 12, 0, 0, tzinfo=SHANGHAI)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt


@dataclass
class _Job:
    job_id: str
    content: str
    run_at: datetime
    group: str
    status: str = "pending"  # pending / sent / skipped
    sent_at: Optional[datetime] = None


class Scheduler:
    def __init__(self, clock=None, store=None, catch_up_window_seconds: int = CATCH_UP_WINDOW_DEFAULT):
        if catch_up_window_seconds < 0 or catch_up_window_seconds > 1800:
            raise ValueError("补发窗口须在 0～1800 秒内")
        self.clock = clock or FakeClock()
        self.store = store if store is not None else {}
        self.catch_up_window_seconds = catch_up_window_seconds
        self._jobs: Dict[str, _Job] = self.store.setdefault("jobs", {})

    def set_clock(self, clock) -> None:
        self.clock = clock

    def _now(self, now=None) -> datetime:
        return now or self.clock.now()

    def add_announcement(self, job_id, content, run_at, group) -> None:
        if job_id not in self._jobs:
            self._jobs[job_id] = _Job(job_id, content, run_at, group)
        else:
            # 不覆盖已发送状态
            job = self._jobs[job_id]
            if job.status == "pending":
                job.content = content
                job.run_at = run_at
                job.group = group

    def tick(self, now=None) -> List[_Job]:
        now = self._now(now)
        due = []
        for job in self._jobs.values():
            if job.status != "pending":
                continue
            if now >= job.run_at:
                due.append(job)
        return due

    def mark_sent(self, job_id, sent_at) -> None:
        job = self._jobs[job_id]
        if job.status == "sent":
            return
        job.status = "sent"
        job.sent_at = sent_at

    def mark_skipped(self, job_id) -> None:
        job = self._jobs[job_id]
        if job.status == "sent":
            return
        job.status = "skipped"

    def on_recover(self, now=None) -> List[_Job]:
        now = self._now(now)
        out = []
        for job in self._jobs.values():
            if job.status != "pending":
                continue
            if now < job.run_at:
                continue
            delta = (now - job.run_at).total_seconds()
            if delta <= self.catch_up_window_seconds:
                out.append(job)
            else:
                job.status = "skipped"
        return out


# ---------------------------------------------------------------------------
# 限流 / 拟人化 / 发送
# ---------------------------------------------------------------------------

def humanize_delay_range():
    return HUMANIZE_DEFAULT


def humanize_delay(text: str = "", *, rng: Optional[random.Random] = None) -> float:
    """拟人延迟：基础抖动 + 按文本长度的打字时间。不真实 sleep，可注入随机源。"""
    rng = rng or random.Random()
    lo, hi = humanize_delay_range()
    base = rng.uniform(lo, hi)
    extra = min(TYPING_DELAY_MAX, (len(text or "") / TYPING_CHARS_PER_SECOND) * 0.25)
    return min(base + extra, hi + TYPING_DELAY_MAX)


def validate_latency_config(p95_max: float = 12.0, process_budget: float = 2.0):
    lo, hi = humanize_delay_range()
    if not (1.5 <= lo < hi <= 8.0):
        raise ValueError(f"拟人化区间非法: {(lo, hi)}")
    if hi + process_budget > p95_max:
        raise ValueError("拟人化上限与 P95 目标不相容")
    return True


class RateLimiter:
    """单群 + 全局限频，夜间静默（主动公告），同内容冷却，随机间隔。"""

    def __init__(
        self,
        *,
        group_per_minute: int = 3,
        global_per_minute: int = 10,
        content_cooldown_seconds: int = 600,
        night_start: str = NIGHT_SILENCE[0],
        night_end: str = NIGHT_SILENCE[1],
        humanize=None,
        rng: Optional[random.Random] = None,
        global_per_hour: int = HOURLY_LIMIT_DEFAULT,
        group_per_hour: int = GROUP_HOURLY_LIMIT_DEFAULT,
        over_limit_policy: str = "drop",
        store=None,
    ):
        self.group_per_minute = group_per_minute
        self.global_per_minute = global_per_minute
        self.content_cooldown_seconds = content_cooldown_seconds
        self.global_per_hour = global_per_hour
        self.group_per_hour = group_per_hour
        self.over_limit_policy = over_limit_policy  # 自动回复超限：drop=降级为不回
        self.night_start = _parse_hhmm(night_start)
        self.night_end = _parse_hhmm(night_end)
        self.humanize = humanize or humanize_delay_range()
        self.rng = rng or random.Random(2026)
        self.store = store if store is not None else {}
        self._events: List[tuple] = self.store.setdefault("rl_events", [])  # (ts, group, kind, content_hash)

    def in_night_silence(self, now: datetime) -> bool:
        now = _ensure_tz(now)
        t = now.timetz().replace(tzinfo=None)
        start, end = self.night_start, self.night_end
        if start <= end:
            return start <= t < end
        # 跨日，如 00:30-07:00 不跨日；若配置 23:00-07:00 则跨日
        return t >= start or t < end

    def allow(self, *, group, content_hash, now, kind="reply") -> bool:
        now = _ensure_tz(now)
        window_start = now - timedelta(minutes=1)
        recent = [e for e in self._events if e[0] > window_start]
        if len(recent) >= self.global_per_minute:
            return False
        if sum(1 for e in recent if e[1] == group) >= self.group_per_minute:
            return False
        hour_start = now - timedelta(hours=1)
        last_hour = [e for e in self._events if e[0] > hour_start]
        if len(last_hour) >= self.global_per_hour:
            return False
        if sum(1 for e in last_hour if e[1] == group) >= self.group_per_hour:
            return False
        if content_hash:
            cool = now - timedelta(seconds=self.content_cooldown_seconds)
            for ts, g, _k, h in self._events:
                if h == content_hash and g == group and ts >= cool:
                    return False
        if kind == "announcement" and self.in_night_silence(now):
            return False
        return True

    def record(self, *, group, content_hash, now, kind="reply") -> None:
        now = _ensure_tz(now)
        self._events.append((now, group, kind, content_hash))

    def next_delay(self) -> float:
        lo, hi = self.humanize
        return self.rng.uniform(lo, hi)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class SendResult:
    ok: bool
    group: str
    text: str
    sent_at: datetime
    delay: float
    skipped: bool = False
    reason: str = ""


class Sender:
    """可记录发送历史的发送层，供测试断言；不触达真实微信。"""

    def __init__(self, limiter: Optional[RateLimiter] = None, clock=None):
        self.limiter = limiter or RateLimiter()
        self.clock = clock or FakeClock()
        self.history: List[SendResult] = []

    def send(self, group, text, *, kind="reply") -> SendResult:
        now = self.clock.now()
        h = content_hash(text)
        delay = self.limiter.next_delay() if kind != "silent" else 0.0
        if not self.limiter.allow(group=group, content_hash=h, now=now, kind=kind):
            res = SendResult(False, group, text, now, delay, True, "rate_limited_or_cooled")
            self.history.append(res)
            return res
        # 模拟：发出时刻 = now + delay（由测试方推进时钟，这里只记录计划延迟）
        self.limiter.record(group=group, content_hash=h, now=now, kind=kind)
        res = SendResult(True, group, text, now, delay, False, "")
        self.history.append(res)
        return res


# ---------------------------------------------------------------------------
# 日志脱敏
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_WXID_RE = re.compile(r"wxid_[A-Za-z0-9_\-]+", re.I)
_TOKEN_RE = re.compile(
    r"(?i)(token|access_token|refresh_token|session_key|api[_-]?key)\s*[=:]\s*([A-Za-z0-9_\-\./+=]+)"
)
_TOKEN_SPACE_RE = re.compile(
    r"(?i)\b(token|access_token|refresh_token|api[_-]?key)\s+([A-Za-z0-9_\-\./+=]{6,})"
)
# 微信号：wx 声明后的短标识，避免误伤普通英文
_WXNAME_RE = re.compile(r"(微信号|微信)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_\-]{5,})")
# 形如 weixin_id / wx: 后的标识
_WX_HANDLE_RE = re.compile(r"(?i)\b(?:weixin_id|wx_id|wx)\s*[=:：]\s*([A-Za-z][A-Za-z0-9_\-]{5,})")


def redact_log(line: str, *, group_names: Optional[list] = None) -> str:
    """对手机号、wxid、微信号、多段 token 做掩码；群名可选脱敏。"""
    s = _PHONE_RE.sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-4:], line or "")
    s = _WXID_RE.sub("wxid_****", s)
    s = _TOKEN_RE.sub(lambda m: m.group(1) + "=" + "****", s)
    s = _TOKEN_SPACE_RE.sub(lambda m: m.group(1) + " ****", s)
    s = _WXNAME_RE.sub(lambda m: m.group(1) + ":" + "****", s)
    s = _WX_HANDLE_RE.sub(lambda m: "wx=****", s)
    if group_names:
        for name in group_names:
            if name:
                s = s.replace(name, "[群名]")
    return s


# ---------------------------------------------------------------------------
# 消息去重（稳定性相关，供扩展）
# ---------------------------------------------------------------------------

class MessageDeduper:
    def __init__(self, store=None):
        self._seen = store if store is not None else set()

    def already_processed(self, msg_id: str) -> bool:
        return msg_id in self._seen

    def ack(self, msg_id: str) -> None:
        self._seen.add(msg_id)


def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=SHANGHAI)
    return dt

# 契约套件与自动回复（从子模块再导出，便于 require_attr / get_impl）
from mock.adapters import (  # noqa: E402
    LocalDbCorruptError,
    LocalDbLockedError,
    LocalDbReader,
    NotificationAssist,
    OcrFallback,
    OcrResult,
    UiaSender,
    UiaSendResult,
    UiaSendStatus,
)
from mock.autoreply import AutoReplyService, ReplyDecision  # noqa: E402

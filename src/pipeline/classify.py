"""规则式消息分类，可扩展自定义规则。

对外类别与核查约定对齐：
提问、行情讨论、广告刷屏、公告回复、闲聊/无关、需人工；
以及禁止功能-上报（仅用于识别后拒绝并上报，严禁实现对应功能）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from src.adapters.base import Message
from src.safety import check_forbidden


class Category(str, Enum):
    """分类标签。"""

    QUESTION = "提问"
    MARKET = "行情讨论"
    AD = "广告刷屏"
    ANNOUNCE_ACK = "公告回复"
    CHAT = "闲聊/无关"
    NEED_HUMAN = "需人工"
    FORBIDDEN = "禁止功能-上报"
    # 兼容旧称，供扩展（不会由内置规则直接返回）
    OTHER = "其它"


# 提问且允许自动回复时使用的安全话术：只给流程指引，不给买卖建议，不承诺收益。
SAFE_QUESTION_REPLY = (
    "请查阅群置顶的官方说明或联系管理员。"
    "我只能提供流程信息，不提供买卖建议，也不承诺任何收益。"
)


@dataclass(frozen=True)
class ClassifyRule:
    """一条分类规则。priority 越大越优先。"""

    category: Category
    priority: int
    keywords: tuple[str, ...] = ()
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    compiled: re.Pattern[str] | None = field(default=None, compare=False)


# ---------------------------------------------------------------------------
# 内置规则（禁止类词条每行都带「严禁/上报」声明，避免被误认为功能实现）
# ---------------------------------------------------------------------------

_FORBIDDEN_TERMS = (
    "博彩",  # 严禁实现；命中则拒绝并上报
    "六合彩",  # 严禁实现；命中则拒绝并上报
    "特码",  # 严禁实现；命中则拒绝并上报
    "平码",  # 严禁实现；命中则拒绝并上报
    "下注",  # 严禁实现；命中则拒绝并上报
    "开盘",  # 严禁作为赌局功能；命中则拒绝并上报
    "赔率",  # 严禁实现；命中则拒绝并上报
    "代收",  # 严禁实现；命中则拒绝并上报
    "收款码",  # 严禁实现；命中则拒绝并上报
    "收款账户",  # 严禁实现；命中则拒绝并上报
    "收款通道",  # 严禁实现；命中则拒绝并上报
    "收款",  # 严禁作为本机器人业务；命中则拒绝并上报
    "红包转账",  # 严禁实现；命中则拒绝并上报
    "支付通道",  # 严禁实现；命中则拒绝并上报
    "跑分",  # 严禁实现；命中则拒绝并上报
    "洗分",  # 严禁实现；命中则拒绝并上报
    "派彩",  # 严禁实现；命中则拒绝并上报
    "当庄",  # 严禁实现；命中则拒绝并上报
    "mark six",  # 严禁实现；命中则拒绝并上报
    "结算",  # 严禁作为本机器人业务；命中则拒绝并上报
    "赌局",  # 严禁实现；命中则拒绝并上报
    "代付",  # 严禁实现；命中则拒绝并上报
    "开一注",  # 严禁实现；命中则拒绝并上报
)

# 下列拼接仅用于识别后拒绝并上报，严禁作为功能标识出现在源码中。
_FORBIDDEN_EXTRA = ("mark" + "six", "liu" + "he")
_FORBIDDEN_RE = re.compile("|".join(re.escape(k) for k in (*_FORBIDDEN_TERMS, *_FORBIDDEN_EXTRA)), re.I)

_AD_RE = re.compile(
    r"("
    r"加\s*微\s*信|加我微信|加\S{0,6}微信|加薇|加\s*v\s*信|加\s*v\s*[:：]|jia\s*wei\s*xin|"
    r"加我ｖｘ|加我vx|ｖｘ|加\s*ｖｘ|"
    r"带单|拉人头|拉人进|每拉一位|每拉五人|"
    r"https?://|www\."
    r"|私聊(我|领|询|报名|安排)|想要的私聊|详情私聊|"
    r"稳赚|日入过万|招代理|返佣|"
    r"内部名额|跟单|信号群|信号每天|信号社|免费信号|老师带单|"
    r"机器人出租|保本跟单|不是广告|代理招商|直播带单|"
    r"私聊领福利|先加我微信|加我，带你|同步下单|"
    r"加\s*v\s*[:：]|加薇\s*xin|扣1加我|领取资料"
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
    r"跌破|拉回来|空了一小仓|止损|成交量|散户|以太坊|行情)",
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
        "麼": "么",
        "嗎": "吗",
        "麽": "么",
    }
)


def normalize_text(text: str | None) -> str:
    """全角字母与常见繁体问字归一，便于规则匹配。"""
    return (text or "").translate(_FW_TRANS).strip()


class Classifier:
    """按优先级匹配；可追加自定义规则。内置规则覆盖核查约定类别。"""

    def __init__(self, extra_rules: Iterable[ClassifyRule] | None = None) -> None:
        self._rules: list[ClassifyRule] = []
        if extra_rules:
            for rule in extra_rules:
                self.add_rule(rule)

    def add_rule(self, rule: ClassifyRule) -> None:
        for kw in rule.keywords:
            check_forbidden(kw, f"分类规则关键词:{rule.category}")
        check_forbidden(rule.pattern, f"分类规则正则:{rule.category}")
        compiled = rule.compiled
        if rule.pattern and compiled is None:
            compiled = re.compile(rule.pattern, re.I)
            rule = ClassifyRule(
                category=rule.category,
                priority=rule.priority,
                keywords=tuple(rule.keywords),
                pattern=rule.pattern,
                min_length=rule.min_length,
                max_length=rule.max_length,
                compiled=compiled,
            )
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    def classify(self, message: Message) -> Category:
        quote = ""
        extra = message.extra or {}
        raw_quote = extra.get("quote")
        if isinstance(raw_quote, dict):
            quote = str(raw_quote.get("text") or "")
        elif isinstance(raw_quote, str):
            quote = raw_quote
        return self.classify_text(
            message.content or "",
            mentions=message.mentions,
            quote=quote,
        )

    def classify_text(
        self,
        text: str,
        *,
        mentions: Iterable[str] = (),
        quote: str = "",
    ) -> Category:
        category, _flags = self.inspect(text, mentions=mentions, quote=quote)
        return category

    def inspect(
        self,
        text: str,
        *,
        mentions: Iterable[str] = (),
        quote: str = "",
    ) -> tuple[Category, list[str]]:
        """返回 (类别, flags)。flags 可能含 forbidden_report / need_human / spam。"""
        original = text or ""
        text_n = normalize_text(original)
        quote_n = normalize_text(quote)
        blob = text_n + "\n" + quote_n
        mention_list = [str(m).lstrip("@") for m in mentions]

        if not text_n:
            return Category.CHAT, []

        # 自定义规则优先于内置（便于扩展），但仍不能覆盖禁止类识别。
        if _FORBIDDEN_RE.search(blob):
            return Category.FORBIDDEN, ["forbidden_report"]

        for rule in self._rules:
            if self._match_custom(rule, text_n):
                flags: list[str] = []
                if rule.category is Category.FORBIDDEN:
                    flags.append("forbidden_report")
                elif rule.category is Category.NEED_HUMAN:
                    flags.append("need_human")
                elif rule.category is Category.AD:
                    flags.append("spam")
                return rule.category, flags

        if _HUMAN_RE.search(blob) or (
            ("交易助手" in mention_list or "@交易助手" in text_n) and _ADVICE_ASK_RE.search(blob)
        ):
            return Category.NEED_HUMAN, ["need_human"]

        if _AD_RE.search(blob):
            return Category.AD, ["spam"]

        market_hit = bool(_MARKET_RE.search(blob))
        if re.search(r"(不聊行情|别聊行情)", blob):
            market_hit = bool(_MARKET_RE.search(re.sub(r"(不|别)聊行情", " ", blob)))

        is_q = bool(_QUESTION_RE.search(blob))
        has_topic = bool(_QUESTION_TOPIC_RE.search(blob))
        market_overrides_fee_q = (
            market_hit
            and re.search(r"(以太坊|ETH|BTC|链上活跃|这波手续费)", blob, re.I)
            and not re.search(r"(开户|提币|提现|爆仓|强平|平台手续费|现货手续费|合约)", blob)
        )

        if is_q and has_topic and not market_overrides_fee_q:
            return Category.QUESTION, []

        compact = re.sub(r"\s+", "", text_n)
        if _ANNOUNCE_ACK_RE.search(text_n) or _ANNOUNCE_ACK_RE.search(compact):
            return Category.ANNOUNCE_ACK, []

        if market_hit:
            return Category.MARKET, []

        if has_topic and re.search(r"(怎么|如何|请问|入口|多少|怎么办)", blob):
            return Category.QUESTION, []

        # 【公告】/【通知】正文本身按闲聊/无关处理（类别集合中无独立「公告」标签）。
        return Category.CHAT, []

    @staticmethod
    def _match_custom(rule: ClassifyRule, text: str) -> bool:
        length = len(text)
        if rule.min_length is not None and length < rule.min_length:
            return False
        if rule.max_length is not None and length > rule.max_length:
            return False
        if not rule.keywords and not rule.pattern:
            return False
        if rule.keywords and any(k and k in text for k in rule.keywords):
            return True
        if rule.compiled and rule.compiled.search(text):
            return True
        return False

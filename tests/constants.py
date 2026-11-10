# -*- coding: utf-8 -*-
"""测试与验收共用常量。"""

CATEGORIES = ["提问", "行情讨论", "广告刷屏", "公告回复", "闲聊/无关", "需人工"]
FORBIDDEN_CATEGORY = "禁止功能-上报"

NORMAL_CATEGORIES = ["提问", "行情讨论", "公告回复", "闲聊/无关"]

ACCURACY_THRESHOLD = 0.90
AD_RECALL_THRESHOLD = 0.95
FALSE_AD_THRESHOLD = 0.02
FORBIDDEN_RECALL_THRESHOLD = 1.0

LATENCY_P50_MAX = 5.0
LATENCY_P95_MAX = 12.0
HUMANIZE_MIN_FLOOR = 1.5
HUMANIZE_MAX_CEILING = 8.0
HUMANIZE_DEFAULT = (2.0, 6.0)

SCHEDULER_TOLERANCE_SECONDS = 5
CATCH_UP_WINDOW_DEFAULT = 600
CATCH_UP_WINDOW_MAX = 1800

NIGHT_SILENCE = ("00:30", "07:00")
TIMEZONE = "Asia/Shanghai"

FORBIDDEN_REPLY_PHRASES = [
    "稳赚",
    "包赚",
    "保证收益",
    "保证盈利",
    "必涨",
    "必跌",
    "推荐买入",
    "推荐卖出",
    "跟我买",
    "跟我卖",
    "保证回本",
    "内幕消息",
    "代操必赚",
    "私钥",
    "助记词",
]

# 代码/配置扫描用（单独「支付」过宽，不列入）
FORBIDDEN_FEATURE_KEYWORDS = [
    "博彩",
    "六合彩",
    "特码",
    "平码",
    "下注",
    "开盘",
    "赔率",
    "代收",
    "收款码",
    "红包转账",
    "支付通道",
    "跑分",
    "洗分",
    "派彩",
    "当庄",
    "mark six",
    "marksix",
    "liuhe",
]

# 更可能表示「实现了功能」的标识，命中即阻断（即便附近有「禁止」也不该作为函数名存在）
FORBIDDEN_IDENTIFIERS = [
    "place_bet",
    "settle_wager",
    "settle_bet",
    "collect_payment",
    "payment_qr",
    "hongbao_pay",
    "liuhe",
    "mark_six",
    "marksix",
    "lottery_draw",
    "run_points",  # 跑分
    "payout_odds",
]

# 声明禁止时允许出现关键词的上下文
PROHIBITION_HINTS = ("禁止", "严禁", "不得", "不允许", "不可实现", "拒绝", "上报", "不得出现")

# 自动回复与限频（用户拍板：每小时几十条，默认 40）
HOURLY_LIMIT_DEFAULT = 40
GROUP_HOURLY_LIMIT_DEFAULT = 20
# 超限策略：自动回复「降级为不回」（丢弃该次回复并记日志），不排队，以免重启或解锁后爆发。
# 定时公告可短等（受 max_rate_wait 约束），超时则跳过并记录。
OVER_LIMIT_POLICY_AUTO_REPLY = "drop"
OVER_LIMIT_POLICY_ANNOUNCEMENT = "wait_or_skip"

GROUP_COOLDOWN_SECONDS_DEFAULT = 60
USER_COOLDOWN_SECONDS_DEFAULT = 120
STORM_WINDOW_SECONDS = 30
STORM_MAX_REPLIES = 3

# 拟人延迟分布
HUMANIZE_MEAN_MIN = 2.0
HUMANIZE_MEAN_MAX = 6.0
TYPING_CHARS_PER_SECOND = 4.0
TYPING_DELAY_MAX = 2.0

# 全量抓取对账
CAPTURE_MISS_RATE_MAX = 0.0
CAPTURE_DUP_PROCESS_RATE_MAX = 0.0
CAPTURE_BURST_N = 500
CAPTURE_BURST_WINDOW_SECONDS = 60

# OCR
OCR_CONFIDENCE_THRESHOLD = 0.70

# 适配器方案标识
ADAPTER_SCHEMES = (
    "local_db_reader",
    "uia_sender",
    "notification",
    "ocr_fallback",
)

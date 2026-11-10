"""日志脱敏与安全的配置展示：手机号、wxid、邮箱、token/key 等不会以明文出现在日志中。"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

# 中国大陆手机号（11 位）。
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 微信号协议 id。
_WXID_RE = re.compile(r"wxid_[a-zA-Z0-9_-]+", re.IGNORECASE)
# 邮箱。
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 赋值形式的密钥：token= / api_key: / secret= / WECHAT_LOCAL_DB_KEY= / *_KEY= / key: 等。
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|authorization|token|bearer|"
    r"wechat_local_db_key|db[_-]?key|[A-Za-z0-9_]*_key|key)"
    r"\s*[:=]\s*([^\s,;，。]+)"
)
# Bearer 头。
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-+/=]{8,})")
# 微信号：声明后的短标识。
_WXNAME_RE = re.compile(r"(微信号|微信)\s*[:：]?\s*([A-Za-z][A-Za-z0-9_\-]{5,})")
# token 后的第二段，如 “token xxx yyy” 中的 xxx。
_TOKEN_SPACE_RE = re.compile(r"(?i)\b(access[_-]?token|refresh[_-]?token|api[_-]?key|token)\s+([A-Za-z0-9._+/=-]{6,})")

# 键名匹配：含 token/secret，以及精确的 key、*_key、db_key、WECHAT_LOCAL_DB_KEY。
# 不用裸子串 "key"，以免把 keywords 等配置项误遮蔽。
_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|api[_-]?key|private[_-]?key|"
    r"appsecret|wxid|session|db[_-]?key|wechat_local_db_key|(?:^|_)key$)",
    re.IGNORECASE,
)

_PLACEHOLDER = {
    "phone": lambda s: s[:3] + "****" + s[-4:],
    "wxid": "wxid_***",
    "email": lambda s: _mask_email(s),
    "secret": "***",
}


def _mask_email(value: str) -> str:
    name, _, domain = value.partition("@")
    if not domain:
        return "***"
    keep = name[0] if name else "*"
    return f"{keep}***@{domain}"


def redact(text: str | None) -> str:
    """对一段文本做脱敏，可供日志或调试输出使用。"""
    if not text:
        return "" if text is None else text

    def _phone(match: re.Match[str]) -> str:
        return _PLACEHOLDER["phone"](match.group(1))

    out = _PHONE_RE.sub(_phone, text)
    out = _WXID_RE.sub(str(_PLACEHOLDER["wxid"]), out)
    out = _EMAIL_RE.sub(lambda m: _mask_email(m.group(0)), out)

    def _secret(match: re.Match[str]) -> str:
        return f"{match.group(1)}={_PLACEHOLDER['secret']}"

    # 先处理 Bearer，避免 Authorization: Bearer xxx 被赋值规则只吃掉 Bearer 一词。
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_PLACEHOLDER['secret']}", out)
    out = _SECRET_ASSIGN_RE.sub(_secret, out)
    out = _WXNAME_RE.sub(lambda m: f"{m.group(1)}:****", out)
    out = _TOKEN_SPACE_RE.sub(lambda m: f"{m.group(1)} ***", out)
    return out


def mask_config(data: Any, *, key: str | None = None) -> Any:
    """递归复制配置结构，敏感键的值替换为 ***，字符串值也会做文本脱敏。"""
    if key and _SENSITIVE_KEY_RE.search(key):
        return "***"
    if isinstance(data, Mapping):
        return {k: mask_config(v, key=str(k)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_config(item) for item in data]
    if isinstance(data, tuple):
        return tuple(mask_config(item) for item in data)
    if isinstance(data, str):
        return redact(data)
    return data


def safe_config_text(data: Mapping[str, Any] | None) -> str:
    """把配置转成可写入日志的文本（已脱敏）。"""
    import json

    masked = mask_config(data or {})
    return json.dumps(masked, ensure_ascii=False, indent=2, default=str)


class RedactFilter(logging.Filter):
    """对日志记录的消息与参数做脱敏。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 先格式化再脱敏，避免把模板里的 token=%s 改成 token=*** 导致 TypeError。
        try:
            formatted = record.getMessage()
        except Exception:
            if record.args:
                if isinstance(record.args, Mapping):
                    record.args = {
                        k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            return True
        record.msg = redact(formatted) if isinstance(formatted, str) else formatted
        record.args = ()
        return True


def setup_logging(level: str | int = "INFO", *, name: str = "wechat_bot") -> logging.Logger:
    """配置带脱敏过滤器的日志记录器。"""
    logger = logging.getLogger(name)
    numeric = getattr(logging, str(level).upper(), None) if isinstance(level, str) else level
    if not isinstance(numeric, int):
        numeric = logging.INFO
    logger.setLevel(numeric)
    if not any(isinstance(f, RedactFilter) for h in logger.handlers for f in h.filters) and not any(
        isinstance(f, RedactFilter) for f in logger.filters
    ):
        logger.addFilter(RedactFilter())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        handler.addFilter(RedactFilter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger

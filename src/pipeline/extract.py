"""要点提取：摘要、关键词、@提及、链接。不依赖分词库，采用规则切分。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.adapters.base import Message

_URL_RE = re.compile(r"(https?://[^\s<>\]\)]+|www\.[^\s<>\]\)]+)", re.I)
_MENTION_RE = re.compile(r"@([^\s@]+)")
_PUNCT = r"\s,，.。!！?？;；:：、~/\\()（）\[\]【】{}<>" + "\"" + "\x27" + "`"
_SPLIT_RE = re.compile("[" + _PUNCT + "]+")
_CN_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")

_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "我", "我们", "你", "你们", "他", "他们",
        "她", "它们", "怎么", "如何", "这个", "那个", "有没有", "啊", "吧",
        "呢", "吗", "哦", "呀", "以及", "和", "或", "但", "与", "及",
        "为了", "可以", "就是", "还是", "那么", "什么", "哪个", "一下",
        "一个", "没有", "不是", "不会", "不能", "自己", "现在", "已经",
        "如果", "因为", "所以", "然后", "这个", "真的", "觉得", "知道",
        "请问", "大家", "各位", "今天", "明天", "一下", "这里", "那里",
        "the", "and", "for", "you", "are", "this", "that",
    }
)


@dataclass(frozen=True)
class Extraction:
    """一条消息的要点。"""

    summary: str
    keywords: tuple[str, ...] = ()
    mentions: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "keywords": list(self.keywords),
            "mentions": list(self.mentions),
            "links": list(self.links),
        }


class Extractor:
    """从正文提取摘要、关键词、提及与链接。"""

    def __init__(self, summary_max_chars: int = 80, max_keywords: int = 8) -> None:
        self.summary_max_chars = summary_max_chars
        self.max_keywords = max_keywords

    def extract(self, message: Message) -> Extraction:
        text = message.content or ""
        links = self._links(text)
        mentions = self._mentions(text, message.mentions)
        keywords = self._keywords(text)
        summary = self._summary(text)
        return Extraction(
            summary=summary,
            keywords=keywords,
            mentions=mentions,
            links=links,
        )

    def _summary(self, text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return ""
        # 优先取首句。
        for sep in ("。", "！", "？", ".", "!", "?"):
            if sep in compact:
                first = compact.split(sep, 1)[0].strip()
                if first:
                    compact = first + sep
                    break
        if len(compact) <= self.summary_max_chars:
            return compact
        return compact[: self.summary_max_chars].rstrip() + "…"

    def _links(self, text: str) -> tuple[str, ...]:
        found: list[str] = []
        for raw in _URL_RE.findall(text):
            cleaned = raw.rstrip(".,;:，。！？、)")
            if cleaned and cleaned not in found:
                found.append(cleaned)
        return tuple(found)

    def _mentions(self, text: str, preset: tuple[str, ...]) -> tuple[str, ...]:
        found: list[str] = []
        for item in preset:
            name = item.lstrip("@").strip()
            if name and name not in found:
                found.append(name)
        for item in _MENTION_RE.findall(text):
            name = item.strip()
            if name and name not in found:
                found.append(name)
        return tuple(found)

    def _keywords(self, text: str) -> tuple[str, ...]:
        # 去掉链接，避免域名污染关键词。
        cleaned = _URL_RE.sub(" ", text)
        tokens: list[str] = []
        for part in _SPLIT_RE.split(cleaned):
            part = part.strip().lstrip("@#")
            if not part:
                continue
            if _EN_WORD_RE.fullmatch(part):
                if part.lower() not in _STOPWORDS:
                    tokens.append(part)
                continue
            # 中文连续片段中再切出 2~6 字的窗口过长，改为整段若长度合适则保留，
            # 同时用汉字扫描提取 2 字及以上的子串边界（按标点已切开）。
            if _CN_WORD_RE.fullmatch(part):
                if 2 <= len(part) <= 8 and part not in _STOPWORDS:
                    tokens.append(part)
                elif len(part) > 8:
                    tokens.extend(self._slide_cn(part))
                continue
            # 中英混合：分别提取
            tokens.extend(
                w for w in _CN_WORD_RE.findall(part) if w not in _STOPWORDS and len(w) <= 8
            )
            tokens.extend(
                w for w in _EN_WORD_RE.findall(part) if w.lower() not in _STOPWORDS
            )
        # 保序去重，优先较长词
        unique: list[str] = []
        seen: set[str] = set()
        for tok in sorted(tokens, key=lambda x: (-len(x), tokens.index(x))):
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(tok)
            if len(unique) >= self.max_keywords:
                break
        return tuple(unique)

    def _slide_cn(self, part: str) -> list[str]:
        """过长中文串按 2~4 字窗口取开头若干候选，避免整句当关键词。"""
        out: list[str] = []
        for n in (4, 3, 2):
            if len(part) >= n:
                cand = part[:n]
                if cand not in _STOPWORDS:
                    out.append(cand)
        return out

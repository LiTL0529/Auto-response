"""企业微信的文本消息不渲染 Markdown：发出去之前把常见的 Markdown 标记转成纯文本。"""

from __future__ import annotations

import re

_URL = re.compile(r"https?://[^\s<>（）]+")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_FENCE = re.compile(r"^\s*```.*$", re.M)
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.M)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![A-Za-z0-9])__(?=\S)(.+?)(?<=\S)__(?![A-Za-z0-9])")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_CODE = re.compile(r"`([^`\n]+)`")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_BLANKS = re.compile(r"\n{3,}")


def plain_text(text: str) -> str:
    """去掉 Markdown 标记，保留文字和链接。链接本身不做任何改动。"""
    if not text:
        return text
    text = _LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）", text)
    urls: list[str] = []

    def keep(m: re.Match) -> str:
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"

    text = _URL.sub(keep, text)
    text = _FENCE.sub("", text)
    text = _RULE.sub("", text)
    text = _TABLE_SEP.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub(r"\1・", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    text = "\n".join(
        line.strip().strip("|").replace(" | ", "  ").strip() if line.strip().startswith("|") else line.rstrip()
        for line in text.split("\n")
    )
    text = _BLANKS.sub("\n\n", text).strip()
    return re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], text)

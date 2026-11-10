"""词条归一化：去 # / emoji / 标点 / 空格，全半角统一，拉丁转小写。"""

from __future__ import annotations

import string
import unicodedata

# NFKC 之后残留的中日韩标点
_CJK_PUNCT = "、。〃〈〉《》「」『』【】〔〕〖〗〝〞–—‘’‛“”„‟…‧﹏·─"
_EXTRA = " \t\r\n\u3000#"

_DROP = set(string.punctuation) | set(_CJK_PUNCT) | set(_EXTRA)


def _is_emoji(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1F300 <= code <= 0x1FAFF
        or 0x1F000 <= code <= 0x1F2FF
        or 0x2600 <= code <= 0x27BF
        or code in (0xFE0F, 0x200D)
    )


def normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    chars = [ch for ch in t if ch not in _DROP and not _is_emoji(ch)]
    return "".join(chars).strip().lower()


def build_alias_index(alias_map: dict[str, list[str]]) -> dict[str, str]:
    """别名 → 主名（归一后作为 key）。"""
    index: dict[str, str] = {}
    for canonical, aliases in alias_map.items():
        index[normalize(canonical)] = canonical
        for alias in aliases:
            index[normalize(alias)] = canonical
    return index


def resolve(text: str, alias_index: dict[str, str]) -> str | None:
    return alias_index.get(normalize(text))

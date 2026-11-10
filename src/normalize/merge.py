"""跨源合并去重。

规则：
- 按归一后的 term 合并；`platforms` 列表就是「跨平台共振」的证据。
- 数值字段不做跨平台比较，只保留最大值并记录来源。
- 锚点不同不合并（如「染色体」生物 vs 娱乐）。
  ⚠️ 只对**采集时就带锚点**的来源生效（目前是 B站组合搜索）；
  抖音的锚点是在去重之后才标注的，所以抖音侧等价于「按 term 合并」。
"""

from __future__ import annotations

from ..models import Candidate
from .term import normalize


def _merge_key(c: Candidate) -> tuple[str, str]:
    anchor = c.anchors[0] if c.anchors else ""
    return c.term_norm, anchor


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    merged: dict[tuple[str, str], Candidate] = {}

    for c in candidates:
        c.term_norm = c.term_norm or normalize(c.term)
        if not c.term_norm:
            continue
        key = _merge_key(c)
        if key not in merged:
            c.finalize()
            merged[key] = c
            continue

        base = merged[key]
        for p in c.platforms:
            if p not in base.platforms:
                base.platforms.append(p)
        for s in c.sources:
            if s not in base.sources:
                base.sources.append(s)
        for a in c.anchors:
            if a not in base.anchors:
                base.anchors.append(a)
        for t in c.tags:
            if t not in base.tags:
                base.tags.append(t)
        for k, v in c.field.items():
            if isinstance(v, (int, float)) and isinstance(base.field.get(k), (int, float)):
                base.field[k] = max(base.field[k], v)
            elif k not in base.field:
                base.field[k] = v
        if base.url is None:
            base.url = c.url
        if base.rank is None or (c.rank is not None and c.rank < base.rank):
            base.rank = c.rank if c.rank is not None else base.rank

    out = list(merged.values())
    for c in out:
        c.tags = c.tags[:20]
    return out

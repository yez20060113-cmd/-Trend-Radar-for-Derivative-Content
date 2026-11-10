"""文案簇识别：找「同一句话被大量复制」的热点。

这是与「素材卡」并列的另一条产出线：
- 素材卡：一个可复用的歌 / 模板 / 挑战
- 文案簇：一句被批量复制的文案（常为歌词），跨领域通用

判定用字符 n-gram 包含度，不依赖模型。

⚠️ 这一类输出最容易出假阳性，所以有三道闸门（都是踩过的坑）：

1. **不是宣发贴**：`to the moon :) w/ #艺人A` × 4 条曾被判成文案簇，
   实际是同一首歌的多组联名宣发——正文是英文、含 `w/`、且命中歌名。
2. **不是同一素材**：正文命中榜单歌名 / 素材名的，归素材卡，不算文案梗。
3. **还活着**：成员发布时长中位数 ≤7 天，否则就是宣传期残留。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from ..models import Candidate

HASHTAG_RE = re.compile(r"#[^\s#]{1,20}")
AT_RE = re.compile(r"@[^\s@]{1,20}")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF\U00002600-\U000027BF\uFE0F\u200D]"
)

# 归一化时丢掉的标点（中英文混在一起，用集合过滤，避免正则转义问题）
_DROP_CHARS = set(
    "，。！？、：；“”‘’（）【】《》「」,.!?:;\"'()[]~`·…—-"
    "\u3000 \t\r\n"
)


@dataclass
class CaptionCluster:
    rep: str                                   # 代表文案
    variants: list[str] = field(default_factory=list)
    members: list[dict] = field(default_factory=list)   # {url, caption, likes, author}
    authors: int = 0
    tags: list[str] = field(default_factory=list)
    likes_max: float = 0
    likes_median: float = 0
    first_index: int = 0

    @property
    def size(self) -> int:
        return len(self.members)


def normalize_caption(text: str) -> str:
    t = HASHTAG_RE.sub("", text or "")
    t = AT_RE.sub("", t)
    t = EMOJI_RE.sub("", t)
    t = "".join(ch for ch in t if ch not in _DROP_CHARS)
    return t.strip()


def clean_display(text: str) -> str:
    t = HASHTAG_RE.sub("", text or "")
    t = AT_RE.sub("", t)
    t = EMOJI_RE.sub("", t)
    return " ".join(t.split()).strip(" ，、。!！?？")


def _shingles(s: str, n: int = 3) -> set[str]:
    if len(s) <= n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _containment(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fa5")


def _eligible(
    c: Candidate, norm: str, song_names: set[str], material_names: set[str], min_chars: int
) -> bool:
    """单条内容够不够格参与文案聚类。"""
    cjk = _cjk_count(norm)
    if cjk < min_chars:
        return False
    # 英文宣发贴：中文占比过低
    if len(norm) and cjk / len(norm) < 0.6:
        return False
    # 「w/ 艺人」是联名宣发的写法，不是文案梗
    if "w/" in (c.term or "").lower():
        return False
    low = norm.lower()
    # 命中歌名 / 素材名 → 归素材卡，不算文案梗
    if any(name and name.lower() in low for name in song_names):
        return False
    if any(name and name.lower() in low for name in material_names):
        return False
    return True


def cluster_captions(
    candidates: list[Candidate],
    *,
    threshold: float = 0.55,
    min_size: int = 3,
    min_chars: int = 8,
    min_authors: int = 2,
    max_age_days: float = 7,
    only_sources: set[str] | None = None,
    song_names: set[str] | None = None,
    material_names: set[str] | None = None,
) -> list[CaptionCluster]:
    song_names = {s for s in (song_names or set()) if s}
    material_names = {s for s in (material_names or set()) if s}
    items = []
    for idx, c in enumerate(candidates):
        if only_sources and c.source not in only_sources:
            continue
        norm = normalize_caption(c.term)
        if len(norm) < min_chars or not _eligible(
            c, norm, song_names, material_names, min_chars
        ):
            continue
        age = c.field.get("age_days")
        items.append(
            {
                "idx": idx,
                "norm": norm,
                "sh": _shingles(norm),
                "caption": c.term,
                "url": c.url or "",
                "likes": float(c.field.get("likes") or 0),
                "author": c.author or "",
                "age": float(age) if isinstance(age, (int, float)) else None,
                "tags": list(c.tags),
            }
        )

    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _containment(items[i]["sh"], items[j]["sh"]) >= threshold:
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)

    out: list[CaptionCluster] = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        # ≥2 个不同作者：挡掉同一账号刷同款文案
        if len({m["author"] for m in members if m["author"]}) < min_authors:
            continue
        # 还活着：成员发布时长中位数 ≤ max_age_days
        ages = sorted(m["age"] for m in members if m["age"] is not None)
        if ages and ages[len(ages) // 2] > max_age_days:
            continue

        members.sort(key=lambda m: m["likes"], reverse=True)
        # 代表文案：出现次数最多的写法；并列时取最短
        counts = Counter(m["norm"] for m in members)
        best_norm = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
        rep = clean_display(next(m["caption"] for m in members if m["norm"] == best_norm))
        likes = sorted(m["likes"] for m in members)
        tags = Counter(t for m in members for t in m["tags"]).most_common(6)
        out.append(
            CaptionCluster(
                rep=rep,
                variants=[clean_display(m["caption"]) for m in members[:6]],
                members=[
                    {"url": m["url"], "caption": m["caption"], "likes": m["likes"]}
                    for m in members[:6]
                ],
                authors=len({m["author"] for m in members if m["author"]}),
                tags=[t for t, _ in tags],
                likes_max=likes[-1] if likes else 0,
                likes_median=likes[len(likes) // 2] if likes else 0,
                first_index=members[0]["idx"],
            )
        )

    out.sort(key=lambda c: (-c.size, -c.likes_max))
    return out

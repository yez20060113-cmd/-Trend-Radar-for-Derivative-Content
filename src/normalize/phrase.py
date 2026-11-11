"""复用短语检测：从一堆文案里找出「被多条视频共同使用的短句」。

为什么不用整句聚类：
- 整句聚类要求文案几乎一字不差，实际只会抓到「宣发贴」和「真的在抄」两类
- 而文案梗常常是「半句相同、后半句各写各的」，整句相似度掉到阈值以下

所以先做短语级统计：一个 4~10 字的中文短语，如果出现在 ≥N 条**不同视频**里，
它就是「疑似文案梗」，值得人工看一眼。它不直接进日报热点，只作发现线索。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from statistics import median

from ..judge.domain import DOMAIN_NOISE, HARD_NOISE_GROUPS, hit as domain_hit
from ..judge.works import looks_like_title, works_ratio
from ..models import Candidate

HASHTAG_RE = re.compile(r"#[^\s#]{1,20}")
AT_RE = re.compile(r"@[^\s@]{1,20}")
NON_CJK_RE = re.compile(r"[^\u4e00-\u9fa5]+")

MIN_LEN = 4
MAX_LEN = 10
DEFAULT_MIN_VIDEOS = 3

# 新鲜度权重：按「使用它的内容年龄中位数」打折。
# 一条老梗只要今天还有 3 条在用，光看条数就能上榜（2026-11-11 用户反馈：
# 「你就这样闯进我」「这么晚了你在干什么」都是几个月前的梗）。
# 中位年龄越小说明这句子最近才被大量复用，越可能是新起的梗。
_FRESH_TIERS = ((3, 1.0), (7, 0.85), (14, 0.6))
_STALE_WEIGHT = 0.4
_AGE_CAP = 40          # 每个短语最多记多少个年龄样本，防内存膨胀

# 硬噪声领域的词（体育/时政/电竞）：整条内容直接不参与
_HARD_WORDS = {
    w for group, words in DOMAIN_NOISE.items() if group in HARD_NOISE_GROUPS for w in words
}


def _body(text: str) -> str:
    t = HASHTAG_RE.sub(" ", text or "")
    t = AT_RE.sub(" ", t)
    t = NON_CJK_RE.sub(" ", t)
    return " ".join(t.split())


def _noise_terms() -> set[str]:
    terms: set[str] = set()
    for words in DOMAIN_NOISE.values():
        terms.update(w for w in words if len(w) >= 2 and w.isascii() is False)
    return terms


def top_phrases(
    candidates: list[Candidate],
    *,
    exclude_names: set[str] | None = None,
    exclude_phrases: set[str] | None = None,
    labels: dict[str, str] | None = None,
    ignore_works: bool = False,
    min_videos: int = DEFAULT_MIN_VIDEOS,
    limit: int = 8,
    subject_names: set[str] | None = None,
) -> list[dict]:
    """返回 [{phrase, videos, likes_max, sample, ...}]，按覆盖视频数 × 新鲜度降序。

    `subject_names` 用来算 `circle_ratio`：这个短语出现在多少条「提到关注主体」的内容里。
    纯跨圈的句子（ratio=0）可能是圈外专用梗，也可能是刚起来还没被追星圈采用的新梗，
    两者要区别对待，所以这里只给信号、不做硬过滤。
    """
    exclude_names = {n for n in (exclude_names or set()) if n}
    exclude_phrases = {p for p in (exclude_phrases or set()) if p}
    noise = _noise_terms()

    labels = labels or {}
    counter: Counter[str] = Counter()
    best_like: dict[str, float] = defaultdict(float)
    sample: dict[str, str] = {}
    contexts: dict[str, list[str]] = defaultdict(list)
    ages: dict[str, list[float]] = defaultdict(list)
    circle_hits: Counter[str] = Counter()
    subjects = {s for s in (subject_names or set()) if s}

    for cand in candidates:
        raw = cand.term or ""
        # 英文噪声词按词边界匹配：否则 `ig` 会命中 `Highlight` 这类正常英文
        low_raw = raw.lower()
        if any(domain_hit(low_raw, w) for w in _HARD_WORDS):
            continue
        body = _body(raw)
        if len(body) < MIN_LEN:
            continue
        likes = float(cand.field.get("likes") or 0)
        age = cand.field.get("age_days")
        age_ok = isinstance(age, (int, float)) and age >= 0
        # 圈内：文案里出现任一关注主体或其别名
        in_circle = bool(subjects) and any(n in raw for n in subjects)
        seen: set[str] = set()
        for n in range(MIN_LEN, MAX_LEN + 1):
            for i in range(len(body) - n + 1):
                gram = body[i : i + n]
                if " " in gram:
                    continue
                seen.add(gram)
        for gram in seen:
            counter[gram] += 1
            if in_circle:
                circle_hits[gram] += 1
            if len(contexts[gram]) < 6:
                contexts[gram].append(cand.term)
            if age_ok and len(ages[gram]) < _AGE_CAP:
                ages[gram].append(float(age))
            if likes > best_like[gram]:
                best_like[gram] = likes
                sample[gram] = cand.term

    cands = []
    for gram, count in counter.items():
        if count < min_videos:
            continue
        label = labels.get(gram, "")
        if label in {"title", "noise"}:
            continue                      # 模型已判定：作品名 / 无关
        if any(name in gram or gram in name for name in exclude_names):
            continue
        if any(word in gram for word in exclude_phrases):
            continue
        if any(w in gram for w in noise):
            continue
        # 规则层：来源文案在讲作品，且短语本身不像一句话 → 判专名
        if not ignore_works and label != "copy" and looks_like_title(gram, contexts.get(gram, [])):
            continue
        cands.append((gram, count))
    # 去碎片：被更长短语包含且计数相差 ≤1 的，丢掉短的
    cands.sort(key=lambda x: (-len(x[0]), -x[1]))
    kept: list[tuple[str, int]] = []
    for gram, count in cands:
        if any(gram in longer and count <= lcount + 1 for longer, lcount in kept):
            continue
        kept.append((gram, count))

    def _age_stats(gram: str) -> tuple[float | None, float | None, float | None]:
        """(中位年龄, 最老年龄, ≤3 天占比)。没有年龄数据时返回三个 None。"""
        a = ages.get(gram) or []
        if not a:
            return None, None, None
        return (
            round(median(a), 1),
            round(max(a), 1),
            round(sum(1 for x in a if x <= 3) / len(a), 2),
        )

    def _fresh_weight(gram: str) -> float:
        med = _age_stats(gram)[0]
        if med is None:
            return 1.0          # 没有年龄数据就不惩罚，别误伤
        for limit, weight in _FRESH_TIERS:
            if med <= limit:
                return weight
        return _STALE_WEIGHT

    # 排序：条数 × 新鲜度。老梗即便条数多，也会被新梗挤下去。
    kept.sort(key=lambda x: -(_fresh_weight(x[0]) * x[1]))
    return [
        {
            "phrase": gram,
            "videos": count,
            "likes_max": best_like.get(gram, 0),
            "sample": sample.get(gram, ""),
            "contexts": contexts.get(gram, [])[:3],
            "works_ratio": round(works_ratio(contexts.get(gram, [])), 2),
            "median_age": _age_stats(gram)[0],
            "max_age": _age_stats(gram)[1],
            "fresh_ratio": _age_stats(gram)[2],
            # 出现在多少条「提到关注主体」的内容里；0 = 纯跨圈
            "circle_hits": circle_hits.get(gram, 0),
            "circle_ratio": round(circle_hits.get(gram, 0) / count, 2) if count else 0.0,
        }
        for gram, count in kept[:limit]
    ]

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

from ..judge.domain import DOMAIN_NOISE, HARD_NOISE_GROUPS, hit as domain_hit
from ..judge.works import looks_like_title, works_ratio
from ..models import Candidate

HASHTAG_RE = re.compile(r"#[^\s#]{1,20}")
AT_RE = re.compile(r"@[^\s@]{1,20}")
NON_CJK_RE = re.compile(r"[^\u4e00-\u9fa5]+")

MIN_LEN = 4
MAX_LEN = 10
DEFAULT_MIN_VIDEOS = 3

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
) -> list[dict]:
    """返回 [{phrase, videos, likes_max, sample}]，按覆盖视频数降序。"""
    exclude_names = {n for n in (exclude_names or set()) if n}
    exclude_phrases = {p for p in (exclude_phrases or set()) if p}
    noise = _noise_terms()

    labels = labels or {}
    counter: Counter[str] = Counter()
    best_like: dict[str, float] = defaultdict(float)
    sample: dict[str, str] = {}
    contexts: dict[str, list[str]] = defaultdict(list)

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
        seen: set[str] = set()
        for n in range(MIN_LEN, MAX_LEN + 1):
            for i in range(len(body) - n + 1):
                gram = body[i : i + n]
                if " " in gram:
                    continue
                seen.add(gram)
        for gram in seen:
            counter[gram] += 1
            if len(contexts[gram]) < 6:
                contexts[gram].append(cand.term)
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

    kept.sort(key=lambda x: -x[1])
    return [
        {
            "phrase": gram,
            "videos": count,
            "likes_max": best_like.get(gram, 0),
            "sample": sample.get(gram, ""),
            "contexts": contexts.get(gram, [])[:3],
            "works_ratio": round(works_ratio(contexts.get(gram, [])), 2),
        }
        for gram, count in kept[:limit]
    ]

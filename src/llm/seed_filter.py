"""热点种子：把聚合榜的热搜标题，变成能去抖音搜的查询词。

两步：
1. `select()` —— 模型判断「这条有没有二创价值」，不做关键词卡点
2. `to_queries()` —— 按类型套转化模板，生成查询词

为什么不让模型直接给查询词：模型容易编出平台上不存在的词。
这里让模型只做「判断 + 抽主体」，查询词由规则拼，可控。

产出的查询词**不是结论**——必须回抖音搜一次看真实使用量，
和「歌名搜回来再看 mentions」是同一个逻辑。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import prompts
from .client import LLM

log = logging.getLogger("radar.llm.seeds")

VALID_TYPES = {"A", "B", "C", "D"}

# 物料类：标题里出现这些词，说明有对应的内容形态，拼对应后缀
_SUFFIX_RULES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("新歌", "单曲", "专辑", "发歌"), ("新歌", "舞台")),
    (("综艺", "节目"), ("名场面", "台词")),
    (("剧", "开播", "定档", "首播"), ("名场面",)),
    (("演唱会", "巡演", "音乐节"), ("演唱会",)),
]

# 玩法名的尾巴：去掉之后再搜，命中率更高
_TAIL_RE = re.compile(r"(开赛了|来袭|火了|刷屏|出圈|上线|定档|官宣|挑战赛|挑战季)$")
_SPLIT_RE = re.compile(r"[，,。！!？?～~]")
# 玩法类必须有这些词之一，否则是模型把「事件」当成了「玩法」
_PLAY_RE = re.compile(r"(挑战|变装|转场|卡点|特效|手势舞|仿妆|合拍|摇)")

MIN_Q, MAX_Q = 2, 20


def _is_sentence(title: str) -> bool:
    """C 类要求是「一句能独立成立的话」。

    热搜标题里常见「人名 人名+动作」这种拼接（如「张小斐 杨幂坐在我的玉腿上」），
    直接当查询词会搜出无关内容，所以带空格的整句一律不要。
    """
    return not re.search(r"[\s　]", title)


def select(
    llm: LLM,
    seeds: list[dict[str, Any]],
    *,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """模型筛出有二创价值的热搜。返回 (保留项, 统计)。

    失败时返回空列表——这只是「找种子」，拿不到就不加查询，不能拖垮跑批。
    """
    picked = [dict(s, idx=i) for i, s in enumerate(seeds[:limit], start=1)]
    stats: dict[str, Any] = {"input": len(picked), "calls": 0, "kept": 0}
    if not picked:
        return [], stats

    system, user = prompts.seed_prompt(picked)
    res = llm.json_call(layer="seed_filter", system=system, user=user)
    stats["calls"] = 1
    if not res.ok:
        stats["failed"] = 1
        log.warning("热点筛选失败：%s", res.error)
        return [], stats

    by_idx = {p["idx"]: p for p in picked}
    data = res.data if isinstance(res.data, dict) else {}
    out: list[dict[str, Any]] = []
    for row in data.get("keep") or []:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("idx"))
        except (TypeError, ValueError):
            continue
        src = by_idx.get(idx)
        typ = str(row.get("type") or "").strip().upper()
        if not src or typ not in VALID_TYPES:
            continue
        out.append(
            {
                "title": src["title"],
                "url": src.get("url", ""),
                "platform": src.get("platform", ""),
                "rank": src.get("rank", 0),
                "heat": src.get("heat", ""),
                "type": typ,
                "subject": str(row.get("subject") or "").strip()[:20],
                "why": str(row.get("why") or "").strip()[:40],
            }
        )
        stats[typ] = stats.get(typ, 0) + 1

    stats["kept"] = len(out)
    if out:
        log.info(
            "热点筛选：%d 条 → 保留 %d（A物料 %d / B玩法 %d / C句子 %d / D主体 %d）",
            len(picked), len(out),
            stats.get("A", 0), stats.get("B", 0), stats.get("C", 0), stats.get("D", 0),
        )
    else:
        log.info("热点筛选：%d 条 → 保留 0", len(picked))
    return out, stats


def _strip_tail(title: str) -> str:
    return _TAIL_RE.sub("", title).strip()


def _one(item: dict[str, Any]) -> list[str]:
    """按类型把一条热搜变成 1~2 个查询词。"""
    title = str(item.get("title") or "").strip()
    typ = item.get("type")
    subj = str(item.get("subject") or "").strip()

    if typ == "B":
        # 玩法：标题里必须真有玩法词。
        # 11-13 实测模型会把「徐峥刷脸惊了」这种事件判成玩法，这里兜一层。
        if not _PLAY_RE.search(title):
            return []
        name = _strip_tail(title)
        return [name] if name else []

    if typ == "C":
        # 句子：取第一个分句的核心，整句太长搜不到
        if not _is_sentence(title):
            return []
        core = _SPLIT_RE.split(title)[0].strip()
        return [core[:12]] if len(core) >= 4 else []

    if typ == "A":
        if not subj:
            return []
        for kws, suffixes in _SUFFIX_RULES:
            if any(k in title for k in kws):
                return [f"{subj} {s}" for s in suffixes]
        # 匹配不到内容形态就别退化成裸主体名——那是 D 类的事，
        # 而且裸名会重复占用配额（「张颂文」和「张颂文 名场面」两条）
        return []

    if typ == "D":
        # 主体：光搜人名回来全是同质内容，加形式词能捞出可复用的玩法
        return [f"{subj} 转场"] if subj else []

    return []


def to_queries(
    items: list[dict[str, Any]],
    *,
    max_queries: int = 8,
) -> list[dict[str, Any]]:
    """把筛选结果转成查询词。去重 + 长度校验，超长的直接丢。

    按类型**轮流**取，不是按顺序取满——11-13 实测：前 8 条全是 A 类物料，
    把「后会友期」这种 C 类热梗挤掉了（它在 kept 里排第 11）。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    by_type: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_type.setdefault(str(it.get("type") or ""), []).append(it)
    for lst in by_type.values():
        lst.sort(key=lambda x: int(x.get("rank") or 999))

    # A（物料）/ C（句子）优先，B（玩法）次之，D（主体）最后
    while len(out) < max_queries:
        before = len(out)
        for typ in ("A", "C", "B", "D"):
            if len(out) >= max_queries:
                break
            bucket = by_type.get(typ) or []
            while bucket:
                it = bucket.pop(0)
                picked = _one(it)
                added = False
                for raw in picked:
                    q = re.sub(r"\s+", " ", raw).strip()
                    if not (MIN_Q <= len(q) <= MAX_Q) or q in seen:
                        continue
                    seen.add(q)
                    out.append(
                        {
                            "q": q,
                            "type": "热种",
                            "from_title": it["title"],
                            "kind": typ,
                            "rank": it.get("rank", 0),
                        }
                    )
                    added = True
                    if len(out) >= max_queries:
                        break
                if added:
                    break       # 这一轮该类取到一个，换下一类
        if len(out) == before:
            break           # 所有类都取不出东西了
    return out

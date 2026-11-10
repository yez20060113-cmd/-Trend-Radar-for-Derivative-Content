"""L2 · 素材理解：从一批文案里读出规则抽不出的「具名素材」。

它补的是 `normalize/entity.py` 的短板：
- 正则只能从引号/书名号里取歌名，模板/挑战靠骨架回溯，切成碎片就废
- `phrase._body()` 会把 #标签 整个剥掉，玩法名藏在线程标签里就看不见
- 短语统计要求 ≥3 条同现，冷启动的素材永远浮不上来

模型读的是「这批人到底在干什么」，不是字面。但它**不是判定层**：
产出的 `EntityHit` 只是多了一个抽取器，仍然要走 aggregate → judge，
门槛还是 mentions ≥2/3。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Candidate, EntityHit
from ..normalize.entity import STOP_NAMES
from ..normalize.pattern import STOP_WORDS as GENERIC_NAMES
from . import prompts
from .client import LLM

log = logging.getLogger("radar.llm.material")

VALID_TYPES = {"模板", "挑战", "BGM"}

# 大类概括词：模型最容易在这里犯错，把它当成了「素材」
# 清单来自 HotspotCriteria §三「反例」+ entity.STOP_NAMES
GENERIC = {
    "变装", "转场", "卡点", "手势舞", "仿妆", "翻跳", "翻唱", "直拍", "混剪",
    "反差变装", "一键换装", "变装挑战", "旅行转场", "慢动作", "多段转场",
    "舞蹈挑战", "舞蹈", "挑战", "模板", "特效", "合拍", "对口型",
}

MIN_CONF = 0.3


@dataclass
class BatchResult:
    """按 url 归集的素材命中，外加一份可写进 run 目录的统计。"""

    hits_by_url: dict[str, list[EntityHit]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    detail: list[dict[str, Any]] = field(default_factory=list)


def _groups(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    """按搜索查询分组。同一条视频只在一条查询下（douyin_search.load 已按 url 去重）。"""
    out: dict[str, list[Candidate]] = {}
    for c in candidates:
        if c.source != "douyin_search" or not c.url:
            continue
        key = (c.combo or "").strip() or "__unknown__"
        out.setdefault(key, []).append(c)
    return out


_TRAIL_PAREN = re.compile(r"[（(]([^（）()]{1,12})[）)]\s*$")
# 括号里是这些词时不要剥：「霸王龙舞步（转场版）」剥完就和「霸王龙舞步（慢动作版）」撞了
_KEEP_PAREN = ("版", "挑战", "舞", "转场", "混音", "remix", "Remix", "live", "Live", "翻唱", "伴奏")


def _clean_name(raw: Any) -> str:
    name = str(raw or "").strip().strip("《》「」“”\"'·.。,，")
    # 模型爱加歌手名做消歧：`DESERVE（BABYMONSTER）`、`我好像在哪见过你（薛之谦）`。
    # 不剥的话同一首歌会变成两张卡（aggregate 按归一名去重），
    # 第一轮回放就是这样凭空多出 2 张假新卡。
    m = _TRAIL_PAREN.search(name)
    if m and not any(k in m.group(1) for k in _KEEP_PAREN):
        trimmed = name[: m.start()].strip()
        if len(trimmed) >= 2:
            name = trimmed
    return name


def _acceptable(name: str, blocked: set[str]) -> str:
    """返回拒绝原因；空串表示通过。"""
    if not name or len(name) < 2:
        return "名字过短"
    if len(name) > 30:
        return "名字过长"
    if name.lower() in STOP_NAMES or name in GENERIC_NAMES or name in GENERIC:
        return "大类概括词"
    if name.lower() in blocked:
        return "撞主体名"
    if name.isdigit():
        return "纯数字"
    return ""


def extract_batch(
    llm: LLM,
    candidates: list[Candidate],
    *,
    blocked: set[str] | None = None,
    max_items: int = 40,
    min_items: int = 3,
    max_calls: int = 30,
) -> BatchResult:
    """对每个查询组调用一次模型。任何一组失败都只跳过该组。"""
    blocked_low = {b.lower() for b in (blocked or set()) if b}
    groups = _groups(candidates)
    result = BatchResult()
    stats = {
        "groups": 0, "calls": 0, "failed": 0,
        "materials": 0, "dropped_evidence": 0, "dropped_name": 0,
    }

    # 条数太少的分组跳过：凑不出 2 条证据，也成不了热点，纯烧钱
    usable = [(q, items) for q, items in groups.items() if len(items) >= min_items]
    usable.sort(key=lambda kv: -len(kv[1]))
    usable = usable[:max_calls]

    failed_queries: list[str] = []

    for query, items in usable:
        payload = [
            {"url": c.url, "likes": float(c.field.get("likes") or 0), "text": c.term}
            for c in items[:max_items]
        ]
        urls = {str(p["url"]) for p in payload}
        system, user = prompts.material_prompt(query=query, items=payload)
        res = llm.json_call(layer="material", system=system, user=user)
        stats["groups"] += 1
        stats["calls"] += 1
        if not res.ok:
            stats["failed"] += 1
            failed_queries.append(query)
            continue

        data = res.data if isinstance(res.data, dict) else {}
        raw_items = data.get("materials") or []
        kept: list[dict[str, Any]] = []
        for m in raw_items:
            if not isinstance(m, dict):
                continue
            name = _clean_name(m.get("name"))
            mtype = str(m.get("type") or "").strip()
            if mtype not in VALID_TYPES:
                stats["dropped_name"] += 1
                continue
            reason = _acceptable(name, blocked_low)
            if reason:
                stats["dropped_name"] += 1
                continue
            # 证据回链：模型给的 url 必须真的在输入里，否则整条作废。
            # 这是防编造的硬约束，不靠提示词。
            evidence = [str(u) for u in (m.get("evidence") or []) if str(u) in urls]
            if not evidence:
                stats["dropped_evidence"] += 1
                continue
            try:
                conf = float(m.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.5
            conf = max(0.0, min(1.0, conf))
            if conf < MIN_CONF:
                continue
            hit = EntityHit(name=name, etype=mtype, confidence=conf, origin="llm")
            for url in evidence:
                result.hits_by_url.setdefault(url, []).append(hit)
            kept.append(
                {
                    "query": query, "name": name, "type": mtype,
                    "confidence": conf, "evidence": evidence,
                    "why": str(m.get("why") or "")[:120],
                }
            )
            stats["materials"] += 1
        if kept:
            result.detail.append({"query": query, "materials": kept})

    result.stats = stats
    if stats["calls"]:
        log.info(
            "L2 素材理解：%d 组 / 调用 %d（失败 %d）｜素材 %d｜证据不实 %d｜名字不合规 %d",
            stats["groups"], stats["calls"], stats["failed"],
            stats["materials"], stats["dropped_evidence"], stats["dropped_name"],
        )
    if failed_queries:
        # 失败必须显式说出来：静默跳过等于当天少采了几组素材，
        # 而且看日报是看不出来的。
        log.warning(
            "L2 有 %d 组调用失败，这些查询下的素材没有被抽取：%s",
            len(failed_queries), "、".join(failed_queries[:6]),
        )
    return result


def apply_to_pairs(
    pairs: list[tuple[Candidate, list[EntityHit]]],
    batch: BatchResult,
) -> int:
    """把模型抽到的素材挂回对应的内容上，返回新增命中数。

    同一条内容上，模型命中与规则命中可能重名；按 (名字, 类型) 去重，
    防止 aggregate 时把一条视频算两遍。
    """
    added = 0
    for cand, hits in pairs:
        extra = batch.hits_by_url.get(str(cand.url or ""))
        if not extra:
            continue
        seen = {(h.name.lower(), h.etype) for h in hits}
        for h in extra:
            key = (h.name.lower(), h.etype)
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
            added += 1
    return added


def register_lifecycle(lifecycle: Any, detail: list[dict[str, Any]], today: str) -> int:
    """把够格的模型素材登记进词龄库，返回登记条数。

    门槛比规则层高：**≥2 条独立证据 + 置信度 ≥0.7**。
    否则模型每轮编一个新词就能污染词龄和跨天复用统计（`judge/history.py` 的
    streak 全靠这个名字对齐，一旦被脏词搅乱，复用治理就失效了）。
    """
    if lifecycle is None:
        return 0
    n = 0
    for group in detail:
        for m in group.get("materials") or []:
            evidence = m.get("evidence") or []
            if len(evidence) >= 2 and float(m.get("confidence") or 0) >= 0.7:
                lifecycle.observe(m["name"], m["type"], today)
                n += 1
    return n

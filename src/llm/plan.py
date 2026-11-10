"""L1 · 查询生成：让模型给出「下一轮该搜什么」。

和实验失败的那条路（从标签共现里重组新词）的区别：
- 那条是**归纳**：我们有什么数据，就从数据里拼什么词，产出受限于已有语料
- 这条是**召回世界知识 + 数据证伪**：模型知道「这个团有哪些歌、哪些名场面、
  哪对 CP」，先给假设，再丢进抖音搜，空结果就是证伪

模型不知道抖音的存量。所以它给的词**一律要联网验证**，验证不通过就丢。
产出落到独立文件，不进当天的 `query_plan.json`，这样它能被单独 A/B。
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import alias_map, subject_profiles
from . import prompts
from .client import LLM

log = logging.getLogger("radar.llm.plan")

VALID_TYPES = {"作品", "别名", "名场面", "CP", "玩法", "事件"}

# 光有形式词没有检索意义（与 entity.STOP_NAMES 同源，这里只要最小的那批）
_TOO_GENERIC = {
    "变装", "转场", "卡点", "直拍", "翻跳", "翻唱", "混剪", "手势舞",
    "仿妆", "舞蹈", "挑战", "模板", "舞台", "合集",
}


def _subject_pools(keywords: dict[str, Any]) -> tuple[list[str], list[str]]:
    profiles = subject_profiles(keywords)
    s_tier, ext = [], []
    for name, info in profiles.items():
        if info.get("type") == "厂牌":
            continue
        if info.get("tier") == "S":
            s_tier.append(name)
        elif info.get("tier") in {"A", "B"}:
            ext.append(name)
    return sorted(s_tier), sorted(ext)


def _chart_lines(chart_songs: list[dict[str, Any]], subjects: set[str]) -> list[str]:
    """只把「歌手是关注主体」的榜单歌给模型看，避免它被无关歌名带跑。"""
    out: list[str] = []
    for song in chart_songs[:60]:
        artists = [str(a) for a in (song.get("artists") or [])]
        if subjects and not any(a in subjects for a in artists):
            continue
        out.append(
            f"{song.get('rank', '')}. {song.get('name', '')} - "
            f"{'、'.join(artists) or '—'}（{song.get('chart', '')}）"
        )
    return out


def _clean_query(raw: Any) -> str:
    return str(raw or "").strip().strip("《》「」“”\"'`。，,?？#")


def generate(
    llm: LLM,
    *,
    keywords: dict[str, Any],
    chart_songs: list[dict[str, Any]],
    used: set[str],
    limit: int = 6,
    date_str: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """返回 (查询列表, 统计)。失败时返回 ([], 统计)，由调用方决定是否回退规则版。"""
    s_tier, ext = _subject_pools(keywords)
    subjects = set(s_tier) | set(ext)
    chart = _chart_lines(chart_songs, subjects)
    system, user = prompts.plan_prompt(
        date_str=date_str,
        subjects_s=s_tier,
        subjects_ext=ext,
        chart=chart,
        used=sorted(used)[:60],
        n=limit,
    )
    res = llm.json_call(layer="plan", system=system, user=user)
    stats: dict[str, Any] = {"calls": 1, "raw": 0, "kept": 0, "dropped": 0}
    if not res.ok:
        stats["failed"] = 1
        return [], stats

    data = res.data if isinstance(res.data, dict) else {}
    rows = data.get("queries") or []
    stats["raw"] = len(rows)

    aliases = alias_map(keywords)
    known = set(subjects)
    for canon, alist in aliases.items():
        known.add(canon)
        known |= {a for a in alist if len(a) >= 2}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        q = _clean_query(row.get("q"))
        low = q.lower()
        if not (2 <= len(q) <= 14):
            stats["dropped"] += 1
            continue
        if q in _TOO_GENERIC or low in used or low in seen:
            stats["dropped"] += 1
            continue
        qtype = str(row.get("type") or "").strip()
        if qtype not in VALID_TYPES:
            qtype = "事件"
        seen.add(low)
        out.append(
            {
                "q": q,
                "type": qtype,
                "reason": f"模型召回：{str(row.get('why') or '')[:40]}",
                "priority": 0.75,
                "subject": str(row.get("subject") or ""),
                "origin": "llm",
            }
        )
        if len(out) >= limit:
            break

    stats["kept"] = len(out)
    if out:
        log.info("L1 查询生成：产出 %d 个（原始 %d，丢弃 %d）", len(out), stats["raw"], stats["dropped"])
    return out, stats

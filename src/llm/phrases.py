"""D 标签层 · 复用短语。

两个不同的动作，别混：

- `label()`    —— 数据清洁：判 copy / title / noise，决定这句话能不能留在候选池。
                  结果写 `phrase_labels.json`，下一轮生效。人工 `--label` 可覆盖。
- `evaluate()` —— 产品判断：留下来的句子，能不能当追星二创文案用、怎么用。
                  只在当天跑一次，结果直接进日报，不落长期状态。

分开是因为前者错了会毁数据，后者错了只是当天少一条。
"""

from __future__ import annotations

import logging
from typing import Any

from . import prompts
from .client import LLM

log = logging.getLogger("radar.llm.phrases")

VALID = {"copy", "title", "noise"}
EVAL_USE = {"use", "watch", "skip"}


def label(
    llm: LLM,
    todo: list[dict[str, Any]],
    *,
    limit: int = 12,
) -> tuple[dict[str, str], dict[str, Any]]:
    """返回 ({短语: 标签}, 统计)。失败返回空，人工流程照旧可用。"""
    items = [t for t in todo if str(t.get("phrase") or "").strip()][:limit]
    stats: dict[str, Any] = {"calls": 0, "input": len(items), "kept": 0}
    if not items:
        return {}, stats

    system, user = prompts.phrase_prompt(items)
    res = llm.json_call(layer="phrase", system=system, user=user)
    stats["calls"] = 1
    if not res.ok:
        stats["failed"] = 1
        return {}, stats

    known = {str(t["phrase"]): t for t in items}
    data = res.data if isinstance(res.data, dict) else {}
    out: dict[str, str] = {}
    for row in data.get("labels") or []:
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase") or "").strip()
        lab = str(row.get("label") or "").strip().lower()
        # 短语必须一字不差地来自输入，防止模型顺手「纠正」了断句残片
        if phrase in known and lab in VALID:
            out[phrase] = lab
    stats["kept"] = len(out)
    if out:
        log.info("D 短语判定：%d 条（copy %d / title %d / noise %d）",
                 len(out),
                 sum(1 for v in out.values() if v == "copy"),
                 sum(1 for v in out.values() if v == "title"),
                 sum(1 for v in out.values() if v == "noise"))
    return out, stats


def evaluate(
    llm: LLM,
    phrases: list[dict[str, Any]],
    *,
    material_names: list[str] | None = None,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """判断每条短语能不能当二创文案用，并给出用法。

    返回 (带 use/usage/source/bgm 的列表, 统计)。失败返回空列表，
    日报退回原来的「复用短语」栏，不影响发信。
    """
    picked = [p for p in phrases if str(p.get("phrase") or "").strip()][:limit]
    stats: dict[str, Any] = {"calls": 0, "input": len(picked), "use": 0, "watch": 0, "skip": 0}
    if not picked:
        return [], stats

    names = [n for n in (material_names or []) if n]
    items: list[dict[str, Any]] = []
    for p in picked:
        row = dict(p)
        # 关联同日素材：看这句出现在哪些文案里，那些文案是否提到今天的热点素材名。
        # 命中就写进 prompt，让模型知道「这句词和哪首歌同期在火」。
        text = " ".join(p.get("contexts") or [])
        hit = [n for n in names if n and n in text]
        row["bgm"] = "、".join(dict.fromkeys(hit))[:40]
        items.append(row)

    system, user = prompts.phrase_eval_prompt(items)
    res = llm.json_call(layer="phrase_eval", system=system, user=user)
    stats["calls"] = 1
    if not res.ok:
        stats["failed"] = 1
        return [], stats

    # 用 items（带 bgm 的副本）而不是 picked：素材关联写在副本上
    known = {str(p["phrase"]): p for p in items}
    data = res.data if isinstance(res.data, dict) else {}
    out: list[dict[str, Any]] = []
    for row in data.get("items") or []:
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase") or "").strip()
        use = str(row.get("use") or "").strip().lower()
        # 原句必须一字不差来自输入：模型爱「顺手改写」，改过的句子是它编的
        if phrase not in known or use not in EVAL_USE:
            continue
        src = known[phrase]
        out.append(
            {
                "phrase": phrase,
                "use": use,
                "usage": str(row.get("usage") or "")[:40],
                "source": str(row.get("source") or "")[:30],
                "videos": src.get("videos", 0),
                "likes_max": src.get("likes_max", 0),
                "sample": (src.get("sample") or "")[:60],
                "bgm": src.get("bgm", ""),
                # 首见于几天前：None 表示今天第一次见（或没有历史）
                "first_seen_days": src.get("first_seen_days"),
                # 圈层来源：circle_hits/videos，0 表示纯跨圈
                "circle_hits": src.get("circle_hits"),
                "circle_ratio": src.get("circle_ratio"),
            }
        )
        stats[use] = stats.get(use, 0) + 1

    # 能用的排前面，同等按视频数
    order = {"use": 0, "watch": 1, "skip": 2}
    out.sort(key=lambda x: (order.get(x["use"], 3), -x["videos"]))
    stats["kept"] = len(out)
    if out:
        log.info(
            "文案评估：%d 条 → use %d / watch %d / skip %d",
            len(picked), stats.get("use", 0), stats.get("watch", 0), stats.get("skip", 0),
        )
    return out, stats

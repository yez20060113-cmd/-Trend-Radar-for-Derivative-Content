"""L3 · 噪声误伤检测：找出「证据很多、却被判成噪声」的素材。

**为什么不再是「边界裁决」**

第一版把「差 1 条到门槛」当边界样本送审，2026-11-09 回放实测：13 个送进去，
模型全判 keep，零改判。原因是那是**证据不足**，不是规则拿不准——模型没本事凭空
变出第 2 条视频。

而规则真正的犯错形态是**成批**的：2026-11-09 早上，`淘汰` / `电竞` 两个多义词
一次性把《SUPERSTAR》（16 条在用）和「后会友期」判成体育/电竞领域丢掉。
这类错误的特征是**信号自相矛盾**——一堆人在用的素材，不可能是无关领域。

所以判据换成：`mentions ≥ min_evidence` 且仍被噪声规则判死。
它平时几乎不触发（今天的正式数据上 0 个），但在词表失灵那天会第一个报警。

**三条不可越的线**
1. 只投 keep / drop 两票，不新增素材、不改素材名
2. `keep` 只是免掉噪声判定，素材仍要过 mentions 门槛和配额
3. 硬领域（体育/电竞）**要送审** —— 11-09 那两次误判正是硬领域造成的
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..judge.material import CIRCLE_IN, _noise_reason
from ..models import Material
from ..normalize.term import normalize as _norm
from . import prompts
from .client import LLM

log = logging.getLogger("radar.llm.adjudicate")

VALID = {"keep", "drop"}

# 证据少于这个数就不送审：1~2 条内容提到，被判噪声也认了，
# 那种量级本来也进不了热点。
DEFAULT_MIN_EVIDENCE = 3


def key_of(m: Material) -> str:
    return f"{_norm(m.name)}|{m.mtype}"


_TRAIL_PAREN = re.compile(r"[（(][^（）()]{1,12}[）)]\s*$")


def _resolve(
    name: str, mtype: str, by_key: dict[str, Material], by_name: dict[str, str]
) -> str:
    """把模型返回的名字对回我们的键。

    模型很爱往名字里塞东西 —— 实测拿到过 `SUPERSTAR（BGM）`，直接把类型写进名字。
    严格匹配会让裁决静默丢失（第一版就是这样：1 条送审、0 条匹配、还显示成功）。
    三级兜底：全等 → 剥括号后全等 → 同名（且唯一）时忽略类型差异。
    """
    exact = f"{_norm(name)}|{mtype}"
    if exact in by_key:
        return exact
    stripped = _TRAIL_PAREN.sub("", name).strip()
    if stripped and stripped != name:
        cand = f"{_norm(stripped)}|{mtype}"
        if cand in by_key:
            return cand
    return by_name.get(_norm(stripped or name), "")


@dataclass
class Verdicts:
    keep: set[str] = field(default_factory=set)
    drop: set[str] = field(default_factory=set)
    detail: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.keep or self.drop)

    @property
    def alerts(self) -> list[dict[str, Any]]:
        """被救回来的素材 = 「噪声词表可能误伤」的证据。"""
        return [d for d in self.detail if d.get("verdict") == "keep"]


def conflicts(
    materials: list[Material],
    *,
    extra_noise: list[str] | None = None,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    limit: int = 25,
) -> list[tuple[Material, str]]:
    """挑出「高证据 + 被判噪声」的素材，返回 (素材, 规则给的噪声理由)。"""
    out: list[tuple[Material, str]] = []
    for m in materials:
        if m.from_chart and m.circle == CIRCLE_IN:
            continue                      # 榜单圈内歌豁免噪声，不会有这个矛盾
        if m.mentions < min_evidence:
            continue                      # 证据不够，误判也认
        reason = _noise_reason(m.name, m.samples, extra_noise)
        if reason:
            out.append((m, reason))
    out.sort(key=lambda x: -x[0].mentions)
    return out[:limit]


def review(
    llm: LLM,
    materials: list[Material],
    *,
    extra_noise: list[str] | None = None,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    limit: int = 25,
) -> Verdicts:
    """返回 keep / drop 两组键。调用失败时返回空 Verdicts，等于没开这一层。"""
    picked = conflicts(
        materials, extra_noise=extra_noise,
        min_evidence=min_evidence, limit=limit,
    )
    result = Verdicts(stats={"picked": len(picked), "calls": 0, "failed": 0})
    if not picked:
        result.stats["note"] = "没有高证据被丢弃的素材"
        return result

    items = [
        {
            "name": m.name,
            "type": m.mtype,
            "circle": m.circle,
            "mentions": m.mentions,
            "anchor_mentions": m.anchor_mentions,
            "chart": m.chart,
            "samples": m.samples,
            "rule_cls": "丢弃",
            "rule_reason": reason,
        }
        for m, reason in picked
    ]
    system, user = prompts.adjudicate_prompt(items)
    res = llm.json_call(layer="adjudicate", system=system, user=user)
    result.stats["calls"] = 1
    if not res.ok:
        result.stats["failed"] = 1
        return result

    by_key = {key_of(m): m for m, _ in picked}
    # 同名卡片可能分属不同类型（同名模板和挑战），只在唯一时才允许忽略类型
    name_count: dict[str, int] = {}
    for m, _ in picked:
        name_count[_norm(m.name)] = name_count.get(_norm(m.name), 0) + 1
    by_name = {
        _norm(m.name): key_of(m) for m, _ in picked if name_count[_norm(m.name)] == 1
    }
    data = res.data if isinstance(res.data, dict) else {}
    for row in data.get("verdicts") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        mtype = str(row.get("type") or "").strip()
        verdict = str(row.get("verdict") or "").strip().lower()
        if verdict not in VALID:
            continue                      # promote 之类的一律不认
        key = _resolve(name, mtype, by_key, by_name)
        if not key:
            continue
        mat = by_key[key]
        (result.keep if verdict == "keep" else result.drop).add(key)
        result.detail.append(
            {
                # 用我们自己的规范名，不用模型写的那个（它可能带「（BGM）」后缀）
                "name": mat.name,
                "type": mat.mtype,
                "verdict": verdict,
                "mentions": mat.mentions,
                "noise_reason": next((r for m, r in picked if m is mat), ""),
                "reason": str(row.get("reason") or "")[:120],
            }
        )

    result.stats.update({"keep": len(result.keep), "drop": len(result.drop)})
    if not result.detail:
        # 模型返回了内容却一条都没对上 —— 这是静默丢结果的高危形态，
        # 第一版就因为 `SUPERSTAR（BGM）` 这种名字吃过一次。必须喊出来。
        result.stats["unmatched"] = len(data.get("verdicts") or [])
        log.warning(
            "L3 裁决返回 %d 条，但没有一条能对上送审素材（名字格式不一致），本次复核作废",
            result.stats["unmatched"],
        )
        return result
    if result.alerts:
        log.warning(
            "L3 报警：%d 张高证据素材被判噪声，模型认为其中 %d 张是误伤 —— %s",
            len(picked), len(result.alerts),
            "、".join(f"{a['name']}({a['mentions']}条)" for a in result.alerts[:5]),
        )
    else:
        log.info("L3 复核：%d 张高证据素材被判噪声，模型确认全部属实", len(picked))
    return result

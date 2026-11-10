"""把「多条内容」聚合成「素材卡」。

同一首歌被 5 条视频用到，不是 5 条输出，而是一张卡 + mentions=5 的热度证据。

⚠️ 圈层判定不能「任一条命中就算圈内」：
「瞬移滑镜慢动作挑战」33 条在用，其中只有 1 条带主体（陈少熙 → 十个勤天），
整张卡被标成「圈内 / 主体=十个勤天」，而它其实是通用转场模板。
所以同时记 `anchor_mentions`，并要求占比达标（默认 ≥20%）。
"""

from __future__ import annotations

import math

from ..constants import TYPE_RANK, TYPE_USAGE
from ..models import Candidate, EntityHit, Material
from ..store.lifecycle import Lifecycle
from .term import normalize

# 圈内证据占比下限：33 条里只 1 条带主体，不该叫「圈内」
CIRCLE_RATIO = 0.2

# 近义变体合并：剥掉骨架后缀后比较「核心词」，核心至少 4 字、长度占比 ≥ 0.6
VARIANT_MIN_LEN = 4
VARIANT_MIN_RATIO = 0.6

# 骨架后缀（长的优先），剥掉后剩下的才是「玩法本体」
_FAMILY_SUFFIXES = (
    "转场挑战", "变装挑战", "手势舞", "进行曲",
    "挑战", "转场", "变装", "换装", "仿妆", "卡点", "合拍", "特效", "滤镜", "大赏", "摇",
)


def aggregate(
    items: list[tuple[Candidate, list[EntityHit]]],
    *,
    today: str,
    lifecycle: Lifecycle | None,
) -> list[Material]:
    mats: dict[tuple[str, str], Material] = {}

    for cand, hits in items:
        seen_here: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.confidence < 0.5:
                continue
            key = (normalize(hit.name), hit.etype)
            # 同一条内容里同名实体只记一次：书名号抽出的《With》与榜单命中会重复
            if key in seen_here:
                continue
            seen_here.add(key)
            mat = mats.get(key)
            if mat is None:
                etype = "挑战" if hit.name.endswith("挑战") else hit.etype
                mat = Material(
                    name=hit.name,
                    mtype=etype,
                    usage_hint=list(TYPE_USAGE.get(etype, [])),
                )
                mats[key] = mat

            if sum(ch.isupper() for ch in hit.name) > sum(ch.isupper() for ch in mat.name):
                mat.name = hit.name
            mat.mentions += 1
            if cand.anchors:
                mat.anchor_mentions += 1
            if cand.source == "douyin_hot":
                mat.from_board = True
            for p in cand.platforms:
                if p not in mat.platforms:
                    mat.platforms.append(p)
            for a in cand.anchors:
                if a not in mat.anchors:
                    mat.anchors.append(a)
            if cand.url and cand.url not in mat.links and len(mat.links) < 3:
                mat.links.append(cand.url)
            if len(mat.samples) < 3:
                mat.samples.append(cand.term)

            play = cand.field.get("play")
            if isinstance(play, (int, float)):
                mat.signals["play_sum"] = mat.signals.get("play_sum", 0) + float(play)

    out = list(mats.values())
    for mat in out:
        need = max(1, math.ceil(mat.mentions * CIRCLE_RATIO))
        if mat.anchor_mentions >= need:
            mat.circle = "圈内"
            mat.artist = mat.anchors[0] if mat.anchors else ""
        else:
            mat.circle = "跨圈"
            mat.artist = ""          # 没有足够的圈内证据，不冒充某个主体
    out.sort(key=lambda m: (TYPE_RANK.get(m.mtype, 9), -m.mentions, m.name))
    return out


def _core(name: str) -> str:
    """剥掉骨架后缀，得到「玩法本体」。"""
    for suf in _FAMILY_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf) + 1:
            return name[: -len(suf)]
    return name


def _is_variant(short: str, long: str) -> bool:
    """两个名字是否是「同一玩法的不同写法」。

    先剥掉骨架后缀再比：
    「霸王龙舞步挑战」→ 霸王龙舞步
    「霸王龙舞步转场挑战」→ 霸王龙舞步转场  ← 前者是后者的前缀 ✓
    「转场挑战」→ 剥完只剩空，直接排除 ✗
    """
    core_s, core_l = _core(short), _core(long)
    if core_s == core_l:
        return True
    if len(core_s) < VARIANT_MIN_LEN or len(core_s) >= len(core_l):
        return False
    if core_s not in core_l:
        return False
    return len(core_s) / len(core_l) >= VARIANT_MIN_RATIO


def merge_variants(materials: list[Material]) -> list[Material]:
    """把同一玩法的不同写法合并成一张卡。

    实测：「霸王龙舞步挑战」(33) 与「霸王龙舞步转场挑战」(9) 曾各占一张，
    同一批视频被拆到两个词上，两个都不够格或重复出现。
    保留名字更长的那条（更具体），把短名的证据并进来，并在 `variants` 里留痕。
    """
    ordered = sorted(materials, key=lambda m: len(m.name), reverse=True)
    kept: list[Material] = []

    for mat in ordered:
        norm = normalize(mat.name)
        target = next(
            (
                k for k in kept
                if k.mtype == mat.mtype
                and k.circle == mat.circle
                and _is_variant(norm, normalize(k.name))
            ),
            None,
        )
        if target is None:
            kept.append(mat)
            continue

        target.mentions += mat.mentions
        target.anchor_mentions += mat.anchor_mentions
        if mat.name not in target.variants:
            target.variants.append(mat.name)
        for a in mat.anchors:
            if a not in target.anchors:
                target.anchors.append(a)
        for p in mat.platforms:
            if p not in target.platforms:
                target.platforms.append(p)
        for link in mat.links:
            if link and link not in target.links and len(target.links) < 3:
                target.links.append(link)
        for s in mat.samples:
            if s and s not in target.samples and len(target.samples) < 3:
                target.samples.append(s)
        target.from_board = target.from_board or mat.from_board
        target.from_chart = target.from_chart or mat.from_chart
        # 榜单信息也要并：否则合并后那张卡会丢掉「新歌榜 #3」这类冷启动证据
        if not target.chart:
            target.chart, target.chart_rank = mat.chart, mat.chart_rank
        elif mat.chart_rank and (not target.chart_rank or mat.chart_rank < target.chart_rank):
            target.chart, target.chart_rank = mat.chart, mat.chart_rank
        if not target.bgm_title and mat.bgm_title:
            target.bgm_title, target.bgm_author = mat.bgm_title, mat.bgm_author
            target.bgm_source = mat.bgm_source

    kept.sort(key=lambda m: (TYPE_RANK.get(m.mtype, 9), -m.mentions, m.name))
    return kept

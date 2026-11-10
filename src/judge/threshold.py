"""内容级数据门槛：进池之前先把"数据不够"的挡掉。

三层（按可用字段自动降级）：
L0 绝对下限（点赞）
L1 相对分位（同批同平台）
L2 素材型指标（收藏率/转发率）—— **未接入**：搜索列表页不返回收藏/转发，
   视频页也没有采集，所以没有可信字段可用（不要假装有）。

"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Candidate


@dataclass
class Threshold:
    min_likes: float = 500.0
    max_age_days: float = 7.0
    percentile: float = 0.40


def _likes(c: Candidate) -> float:
    value = c.field.get("likes")
    return float(value) if isinstance(value, (int, float)) else 0.0


def apply(candidates: list[Candidate], cfg: Threshold) -> tuple[list[Candidate], dict[str, int]]:
    stats = {"input": len(candidates), "drop_age": 0, "drop_likes": 0, "drop_percentile": 0, "kept": 0}

    # L0：绝对下限 + 时效
    stage1: list[Candidate] = []
    for c in candidates:
        age = c.field.get("age_days")
        if isinstance(age, (int, float)) and age > cfg.max_age_days:
            stats["drop_age"] += 1
            continue
        if cfg.min_likes > 0 and _likes(c) < cfg.min_likes:
            stats["drop_likes"] += 1
            continue
        stage1.append(c)

    # L1：同批分位（只有带点赞的候选参与）
    likes = sorted((_likes(c) for c in stage1 if _likes(c) > 0))
    if likes and cfg.percentile > 0:
        idx = max(0, min(len(likes) - 1, int(len(likes) * cfg.percentile) - 1))
        floor = likes[idx]
        kept = [c for c in stage1 if _likes(c) >= floor]
        stats["drop_percentile"] = len(stage1) - len(kept)
        stage1 = kept

    stats["kept"] = len(stage1)
    return stage1, stats

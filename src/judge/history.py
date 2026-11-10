"""跨天复用统计：判断一张卡是「新面孔」还是「连续第 N 天」。

为什么需要这一层：
2026-11-06~09 的日报里，11 个位置有 8 个在复用（NEON、我要逮捕你、瞬移滑镜…），
原因是「计划 → 采集 → 判定」三层都没有新鲜度约束：

- 挑战类查询 100% 来自上一轮 materials，今天搜它、明天它还在
- 歌名类跟着 QQ 榜单走，NEON 连续 4 天在榜
- 判定层的 score 只看 mentions，老卡视频基数大，永远压过新卡

这里只做一件事：给出「过去几天是否出现过、连续几天、上次多少条」，
由 `judge.material` 决定让位还是保留。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..normalize.term import normalize

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_recent_hot(
    base: Path,
    today: str,
    days: int = 4,
) -> dict[str, dict]:
    """读取最近几天归档里「热点」卡的复用信息。

    返回 `{归一化名称: {"streak": 连续天数, "last_mentions": 上次提到数,
                        "last_rank": 上次榜单排名, "last_date": 最近出现的日期}}`

    `streak` 从**昨天**往前连续计数：昨天有、前天有 → 2；今天再出现就是「连续第 3 天」。
    只看 `< today` 的归档，避免把今天早上那版算进历史（重跑时会重复计数）。
    """
    if not base.exists():
        return {}

    dirs = [
        p for p in base.iterdir()
        if p.is_dir() and DATE_RE.match(p.name) and p.name < today
    ]
    dirs.sort(reverse=True)
    history = dirs[: max(1, days)]
    if not history:
        return {}

    daily: dict[str, dict[str, dict]] = {}
    for d in history:
        path = d / "materials.json"
        if not path.exists():
            continue
        try:
            mats = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 历史文件坏了不该影响今天
            continue
        hot: dict[str, dict] = {}
        for m in mats:
            if m.get("cls") != "热点":
                continue
            key = normalize(str(m.get("name") or ""))
            if key:
                hot[key] = m
        daily[d.name] = hot

    if not daily:
        return {}

    latest_date = history[0].name
    latest = daily.get(latest_date, {})
    out: dict[str, dict] = {}
    for key, mat in latest.items():
        streak = 0
        for d in history:
            if key in daily.get(d.name, {}):
                streak += 1
            else:
                break
        out[key] = {
            "streak": streak,
            "last_mentions": int(mat.get("mentions") or 0),
            "last_rank": int(mat.get("chart_rank") or 0),
            "last_date": latest_date,
        }
    return out

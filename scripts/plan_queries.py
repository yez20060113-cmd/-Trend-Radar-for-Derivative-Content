"""生成当日检索计划：把「搜什么」从脚本里抽出来，落成一份可复现的文件。

为什么要有这一层：
- 之前查询由「一年中的第几天 × 2」轮转决定，138 个主体要 69 天才能轮一遍
- 热点生命周期只有几天，轮不到就等于没有
- 计划落盘后，可以复现、可以续采、可以按产出率回头调整

本脚本是**不依赖模型**的版本：只用语库 + 榜单 + 上一轮结果生成。
后续把生成逻辑换成模型时，输出格式不变，采集脚本不用改。

用法：python scripts/plan_queries.py
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))

from src.collectors import douyin_hot  # noqa: E402
from src.collectors.http import Fetcher  # noqa: E402
from src.collectors.qq_music import fetch as fetch_chart  # noqa: E402
from src.config import (  # noqa: E402
    alias_map,
    keyword_layer,
    load_keywords,
    load_sources,
    subject_profiles,
)
from src.constants import TYPE_QUERY  # noqa: E402
from src.judge.anchor import context as anchor_context  # noqa: E402
from src.judge.anchor import match_subject  # noqa: E402

OUT = ROOT / "data" / "input" / "query_plan.json"
RUNS = ROOT / "sandbox" / "runs"


def _latest_run() -> Path | None:
    dirs = sorted([p for p in RUNS.iterdir() if p.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def _chart_queries(kw: dict, limit: int) -> list[dict]:
    """榜单歌名查询：只留「歌手是你关注的主体」的歌。

    主体匹配必须走 `judge.anchor.match_subject`——早期这里用裸子串，
    `礼` 这种单字别名也能命中，于是拿一个误判的艺人名去搜歌，判定层又不认。
    """
    subjects = keyword_layer(kw, "锚点层", "主体")
    amap = alias_map(kw)
    judge_cfg = load_sources().get("judge", {}) or {}
    ctx = anchor_context(kw, judge_cfg)
    qcfg = load_sources().get("sources", {}).get("qq_music", {})
    try:
        songs = fetch_chart(
            [str(c) for c in (qcfg.get("charts") or ["27"])],
            max_rank=int(qcfg.get("max_rank", 30)),
            qps_ms=int(qcfg.get("qps_ms", 800)),
        )["songs"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 榜单读取失败：{exc}")
        return []

    out: list[dict] = []
    for song in songs:
        artist = "、".join(song.get("artists") or [])
        hit = match_subject(artist, subjects, amap, **ctx)
        if not hit:
            continue
        out.append({
            "q": song["name"], "type": "歌名",
            "reason": f"{song.get('chart', '')}#{song.get('rank', '')} · {hit}",
            "priority": 1.0,
        })
        if len(out) >= limit:
            break
    return out


def _previous_challenges(limit: int) -> list[dict]:
    """上一轮跑出来的挑战/模板，按证据强弱排。"""
    run = _latest_run()
    if not run or not (run / "materials.json").exists():
        return []
    materials = json.loads((run / "materials.json").read_text(encoding="utf-8"))
    rows = [m for m in materials if m["mtype"] in {"挑战", "模板"}]
    rows.sort(key=lambda m: (-int(m.get("mentions") or 0), m["name"]))
    return [
        {
            "q": m["name"], "type": "挑战",
            "reason": f"上轮 {m.get('mentions', 0)} 条在用",
            "priority": 0.9 if m.get("cls") == "热点" else 0.6,
        }
        for m in rows[:limit]
    ]


def _hot_challenges(limit: int) -> list[dict]:
    """抖音热榜里的挑战/模板词。

    这一路必须存在：只靠「上轮结果」会形成自我强化——明天搜的永远是今天火的那几个，
    新玩法一条也进不来。文档一直写着「热榜 + 上轮候选」，这里是把它补成真的。
    """
    if limit <= 0:
        return []
    hot_cfg = load_sources().get("sources", {}).get("douyin_hot", {})
    if not hot_cfg.get("enabled"):
        return []
    fetcher = Fetcher("Mozilla/5.0")
    try:
        items, _ = douyin_hot.collect(fetcher, hot_cfg, datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 热榜读取失败：{exc}")
        return []
    finally:
        fetcher.close()

    out: list[dict] = []
    for cand in items:
        title = (cand.term or "").strip()
        if not title.endswith("挑战") or not 4 <= len(title) <= 20:
            continue
        out.append({
            "q": title, "type": "挑战",
            "reason": f"热榜 #{cand.rank or '-'}",
            "priority": 0.8,
        })
        if len(out) >= limit:
            break
    return out


def _challenge_queries(limit: int) -> list[dict]:
    """热榜先占一半，剩下的用上轮结果补齐（避免全是新词、也避免全是旧词）。"""
    hot_n = max(1, limit // 2) if limit else 0
    hot = _hot_challenges(hot_n)
    taken = {q["q"] for q in hot}
    prev = [q for q in _previous_challenges(limit) if q["q"] not in taken]
    return (hot + prev)[:limit]


def _rotate(pool: list[str], day: int, n: int) -> list[str]:
    if not pool or n <= 0:
        return []
    start = (day * max(1, n)) % len(pool)
    return [pool[(start + i) % len(pool)] for i in range(min(n, len(pool)))]


def _subject_queries(kw: dict, limit: int) -> list[dict]:
    """S 档主力 + A/B 档演员与 rapper 轮转，按类型加限定词。

    之前只取 S 档，而 20 个演员全在 A/B 档 → 「演员 搜台词」这条规则从未执行。
    """
    profiles = subject_profiles(kw)
    core = [
        s for s, info in profiles.items()
        if info.get("tier") == "S" and info.get("type") != "厂牌"
    ]
    ext = [
        s for s, info in profiles.items()
        if info.get("tier") in {"A", "B"} and info.get("type") in TYPE_QUERY
    ]
    if not core and not ext:
        return []

    day = datetime.now().timetuple().tm_yday
    n_core = max(1, round(limit * 0.6)) if core else 0
    picked = _rotate(core, day, n_core) + _rotate(ext, day, limit - n_core)

    out: list[dict] = []
    for i, name in enumerate(picked):
        quals = TYPE_QUERY.get(profiles.get(name, {}).get("type", ""))
        text = f"{name} {quals[i % len(quals)]}" if quals else name
        out.append({"q": text, "type": "主体", "reason": "S/A 档轮转", "priority": 0.7})
    return out


def _discovery_queries(used: set[str], limit: int) -> list[dict]:
    """从生命周期记录里挑最近出现、但还没排进计划的词，作为发现入口。

    过滤规则（截断片段曾占满 5 个配额）：
    - 长度 ≥4、出现 ≥2 次
    - 不能是更长词的子串（`izon变装` 是 `babyshorizon变装转场挑战` 的尾巴）
    """
    path = ROOT / "data" / "state" / "term_lifecycle.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    rows = [
        (str(word), info) for word, info in data.items()
        if isinstance(info, dict)
    ]
    # 先按首见时间降序，再按长度降序（稳定排序：长词优先保留）
    rows.sort(key=lambda kv: str(kv[1].get("first_seen", "")), reverse=True)
    rows.sort(key=lambda kv: len(kv[0]), reverse=True)

    kept: list[str] = []
    out: list[dict] = []
    for word, info in rows:
        if len(word) < 4 or word.lower() in used:
            continue
        if int(info.get("count") or 0) < 2:
            continue
        if any(word in longer for longer in kept):
            continue          # 是某个更长词的截断片段
        kept.append(word)
        out.append({
            "q": word, "type": "发现",
            "reason": f"首见 {info.get('first_seen', '')} · {info.get('count', 0)} 次",
            "priority": 0.5,
        })
        if len(out) >= limit:
            break
    return out


def _interleave(batches: list[list[dict]], max_queries: int) -> list[dict]:
    """按类型轮流取，而不是「一类跑完再下一类」。

    2026-11-09 的教训：计划是「歌名 10 → 挑战 10 → 主体 10 → 发现 5」，
    采集在第 10 个查询后就开始 502 熔断，于是日报里 8 张卡全是 BGM、跨圈 0 ——
    不是当天没有新挑战，而是挑战查询根本没轮到。
    交错后，同样的覆盖率会得到各类型都有的样本。
    """
    out: list[dict] = []
    seen: set[str] = set()
    depth = 0
    while len(out) < max_queries:
        added = False
        for batch in batches:
            if depth >= len(batch):
                continue
            item = batch[depth]
            key = item["q"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            added = True
            if len(out) >= max_queries:
                break
        if not added:
            break
        depth += 1
    return out


def main() -> int:
    cfg = load_sources().get("sources", {}).get("douyin_search", {})
    max_queries = int(cfg.get("max_queries", 40))
    scroll = int(cfg.get("scroll", 5))
    quotas = cfg.get("quota") or {"歌名": 15, "挑战": 10, "主体": 10, "发现": 5}

    kw = load_keywords()
    batches = [
        _chart_queries(kw, int(quotas.get("歌名", 15))),
        _challenge_queries(int(quotas.get("挑战", 10))),
        _subject_queries(kw, int(quotas.get("主体", 10))),
    ]
    used = {item["q"].lower() for batch in batches for item in batch}
    batches.append(_discovery_queries(used, int(quotas.get("发现", 5))))

    # 交错：歌名 / 挑战 / 主体 / 发现 轮流上，避免「前面把额度用完、只剩一类」
    queries = _interleave(batches, max_queries)
    plan = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_by": "rule-based",
        "budget": {"max_queries": max_queries, "scroll": scroll},
        "queries": queries,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: dict[str, int] = {}
    for q in queries:
        counts[q["type"]] = counts.get(q["type"], 0) + 1
    print(f"计划 {len(queries)} 个查询（{counts}），每查询滚动 {scroll} 次 → {OUT}")
    for q in queries[:8]:
        print(f"  [{q['type']}] {q['q'][:30]:<32}{q['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

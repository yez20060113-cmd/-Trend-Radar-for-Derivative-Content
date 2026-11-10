"""L1 · 用模型生成检索计划（与规则版并列，不覆盖）。

产出默认写 `data/input/query_plan_llm.json`，**不动** `query_plan.json`。
这样它能被单独启用、单独评分；确认有增益后，再用 `--merge` 并进当天计划。

用法：
    python scripts/llm_plan.py                 # 只生成，看看模型会给什么词
    python scripts/llm_plan.py --dry           # 不联网，只看 prompt 多大、要花多少
    python scripts/llm_plan.py --merge         # 并进 query_plan.json（遵守 max_queries）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.collectors.qq_music import fetch as fetch_chart  # noqa: E402
from src.config import load_keywords, load_sources  # noqa: E402
from src.llm import context as llm_context  # noqa: E402
from src.llm import prompts as llm_prompts  # noqa: E402
from src.llm.client import LLM, probe  # noqa: E402
from src.llm.plan import generate  # noqa: E402

PLAN = ROOT / "data" / "input" / "query_plan.json"
OUT = ROOT / "data" / "input" / "query_plan_llm.json"
RAW = ROOT / "data" / "input" / "douyin_latest.json"
LIFECYCLE = ROOT / "data" / "state" / "term_lifecycle.json"


def _used() -> set[str]:
    """已搜过的词：计划里的 + 实际跑过的。重复搜同一个词是纯浪费。"""
    out: set[str] = set()
    for path, key in ((PLAN, "queries"), (RAW, "queries")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for row in data.get(key) or []:
            q = str(row.get("q") or "").strip()
            if q:
                out.add(q.lower())
    return out


def _chart() -> list[dict]:
    cfg = load_sources().get("sources", {}).get("qq_music", {}) or {}
    try:
        return fetch_chart(
            [str(c) for c in (cfg.get("charts") or ["27"])],
            max_rank=int(cfg.get("max_rank", 30)),
            qps_ms=int(cfg.get("qps_ms", 800)),
        )["songs"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 榜单读取失败（不阻塞）：{exc}")
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="不调用模型，只估算 prompt 与成本")
    ap.add_argument("--merge", action="store_true", help="并进 query_plan.json")
    ap.add_argument("--quota", type=int, default=0, help="覆盖配置里的配额")
    args = ap.parse_args()

    cfg = load_sources()
    llm_cfg = cfg.get("llm", {}) or {}
    layer = (llm_cfg.get("layers", {}) or {}).get("plan", {}) or {}
    quota = args.quota or int(layer.get("quota", 6))

    keywords = load_keywords()
    chart = _chart()
    used = _used()
    date_str = datetime.now().strftime("%Y-%m-%d")

    llm = probe(llm_cfg)     # 手动生成不占用生产开关
    print(f"模型：{llm.model}｜base_url：{llm.base_url}｜状态：{'可用' if llm.available else llm.why_unavailable()}")
    print(f"文档注入：{llm_context.describe()}")

    if args.dry:
        from src.llm.plan import _chart_lines, _subject_pools

        s_tier, ext = _subject_pools(keywords)
        system, user = llm_prompts.plan_prompt(
            date_str=date_str, subjects_s=s_tier, subjects_ext=ext,
            chart=_chart_lines(chart, set(s_tier) | set(ext)),
            used=sorted(used)[:60], n=quota,
        )
        print(f"\n[dry] system {len(system)} 字 / user {len(user)} 字 ≈ "
              f"{(len(system) + len(user)) / 1.6:.0f} tokens｜1 次调用")
        print("\n---- user prompt ----")
        print(user)
        return 0

    if not llm.available:
        print("❌ 模型不可用，无法生成。先配好 key 再跑。")
        return 2

    queries, stats = generate(
        llm, keywords=keywords, chart_songs=chart, used=used,
        limit=quota, date_str=date_str,
    )
    llm.save_cache()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "date": date_str,
                "generated_by": f"llm:{llm.model}",
                "stats": stats,
                "queries": queries,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n生成 {len(queries)} 个查询 → {OUT}")
    for q in queries:
        print(f"  [{q['type']}] {q['q'][:24]:<26}{q['reason']}")

    if args.merge and queries:
        plan = json.loads(PLAN.read_text(encoding="utf-8")) if PLAN.exists() else {}
        max_q = int(
            (cfg.get("sources", {}).get("douyin_search", {}) or {}).get("max_queries", 40)
        )
        existing = plan.get("queries") or []
        have = {str(x.get("q", "")).lower() for x in existing}
        merged = list(existing)
        for q in queries:
            if q["q"].lower() not in have and len(merged) < max_q:
                merged.append(q)
                have.add(q["q"].lower())
        plan["queries"] = merged
        plan["date"] = date_str
        plan["generated_by"] = f"rule-based+llm:{llm.model}"
        PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已并入 {PLAN}（现 {len(merged)}/{max_q} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

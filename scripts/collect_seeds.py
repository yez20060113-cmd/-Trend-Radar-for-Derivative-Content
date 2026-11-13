"""每天生成「热点种子」：抓聚合榜 → 模型筛 → 转成抖音查询词。

产物 `data/input/hot_seeds.json` 由 `plan_queries.py` 读取，作为「热种」类查询。
它**不替代**原有链路：种子只是「今天该搜什么」的一个新来源，
采集、抽取、判定、出报完全不变。

用法：
    python scripts/collect_seeds.py            # 抓 + 筛 + 转
    python scripts/collect_seeds.py --dry      # 只抓不筛，看原始条目
    python scripts/collect_seeds.py --limit 60 # 只筛前 60 条（省 token）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collectors.http import Fetcher  # noqa: E402
from src.collectors.tunbang import DEFAULT_BOARDS, collect  # noqa: E402
from src.config import DATA_DIR, load_sources  # noqa: E402
from src.llm import seed_filter  # noqa: E402
from src.llm.client import probe  # noqa: E402

OUT = DATA_DIR / "input" / "hot_seeds.json"
log = logging.getLogger("seeds")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只抓不筛")
    ap.add_argument("--limit", type=int, default=0, help="只筛前 N 条，0 表示全量")
    ap.add_argument("--out", default="", help="输出路径（默认 data/input/hot_seeds.json）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = load_sources()
    tb = cfg.get("tunbang") or {}
    if not tb.get("enabled", True):
        log.info("tunbang 未启用，跳过")
        return 0

    ua = (cfg.get("collect") or {}).get("user_agent") or "Mozilla/5.0"
    now = datetime.now().isoformat(timespec="seconds")

    with Fetcher(ua, timeout=float(tb.get("timeout_seconds", 20))) as fetcher:
        seeds, _fields = collect(fetcher, tb, now)
    log.info("抓取：%d 条（%s）", len(seeds), "、".join(DEFAULT_BOARDS))

    if not seeds:
        log.warning("一条都没抓到，跳过写盘")
        return 1

    if args.dry:
        for s in seeds[:20]:
            log.info("  %-6s #%-3s %s", s["platform"][:6], s["rank"], s["title"][:40])
        return 0

    limit = args.limit or int(tb.get("filter_limit", 100))
    llm = probe(cfg.get("llm") or {})
    if not llm.available:
        log.warning("模型不可用（%s），只写原始种子、不生成查询", llm.why_unavailable())
        kept, stats, queries = [], {"input": 0, "kept": 0}, []
    else:
        kept, stats = seed_filter.select(llm, seeds, limit=limit)
        queries = seed_filter.to_queries(kept, max_queries=int(tb.get("max_queries", 8)))

    out = Path(args.out) if args.out else OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": now,
                "source": "tunbang",
                "boards": list(DEFAULT_BOARDS),
                "raw_count": len(seeds),
                "stats": stats,
                "kept": kept,
                "queries": queries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info("查询词 %d 个：", len(queries))
    for q in queries:
        log.info("  [%s] %-20s ← %s", q["kind"], q["q"], q["from_title"][:26])
    log.info("已写入 %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

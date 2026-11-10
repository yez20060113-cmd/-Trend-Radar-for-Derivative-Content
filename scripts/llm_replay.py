"""离线回放：拿已经采到的数据跑 L2 / L3，和规则版对照。

**不联网、不采集、不改 data/**：输入是 `sandbox/runs/<ts>/candidates.json`，
输出写到 `sandbox/replay/<ts>/`。删掉目录就等于回滚。

这是切换模型层之前唯一该做的验证 —— 不用等明天 09:00，今天就能看到
「模型多抽出哪些素材、少抽出哪些」。

用法：
    python scripts/llm_replay.py --dry                 # 不调模型，只看要花多少
    python scripts/llm_replay.py                       # 回放 L2 + L3
    python scripts/llm_replay.py --layer material      # 只回放 L2
    python scripts/llm_replay.py --run 20261109-1442   # 指定某一轮
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import alias_map, keyword_layer, load_keywords, load_sources  # noqa: E402
from src.llm import adjudicate as llm_adjudicate  # noqa: E402
from src.llm import context as llm_context  # noqa: E402
from src.llm import material as llm_material  # noqa: E402
from src.llm import prompts as llm_prompts  # noqa: E402
from src.llm.client import LLM, probe  # noqa: E402
from src.models import Candidate, EntityHit, Material  # noqa: E402
from src.normalize import entity  # noqa: E402
from src.normalize.aggregate import aggregate  # noqa: E402

RUNS = ROOT / "sandbox" / "runs"
REPLAY = ROOT / "sandbox" / "replay"


def _latest_run() -> Path | None:
    dirs = sorted([p for p in RUNS.iterdir() if p.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def _load_candidates(run: Path) -> list[Candidate]:
    path = run / "candidates.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate(**r) for r in rows]


def _load_materials(run: Path) -> list[Material]:
    path = run / "materials.json"
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Material(**r) for r in rows]


def _pairs(cands: list[Candidate]) -> list[tuple[Candidate, list[EntityHit]]]:
    return [(c, entity.extract(c)) for c in cands]


def _dry_material(cands: list[Candidate], max_items: int, min_items: int, max_calls: int) -> None:
    groups: dict[str, list[Candidate]] = {}
    for c in cands:
        if c.source != "douyin_search" or not c.url:
            continue
        groups.setdefault((c.combo or "__unknown__"), []).append(c)
    usable = [(q, it) for q, it in groups.items() if len(it) >= min_items]
    usable.sort(key=lambda kv: -len(kv[1]))
    usable = usable[:max_calls]
    chars = 0
    for q, items in usable:
        payload = [
            {"url": c.url, "likes": float(c.field.get("likes") or 0), "text": c.term}
            for c in items[:max_items]
        ]
        system, user = llm_prompts.material_prompt(query=q, items=payload)
        chars += len(system) + len(user)
    print(f"\n[dry] L2：{len(usable)} 个分组需要调用（共 {len(groups)} 组，"
          f"<{min_items} 条的被跳过）")
    print(f"[dry] prompt 合计 {chars} 字 ≈ {chars / 1.6:.0f} tokens 输入")
    print(f"[dry] 输出按每组件 300 tokens 估 ≈ {len(usable) * 300} tokens")
    print(f"[dry] 文档注入：material {llm_context.describe().get('material')} 字")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="", help="运行目录名，缺省用最新一轮")
    ap.add_argument("--layer", default="both", choices=["both", "material", "adjudicate"])
    ap.add_argument("--dry", action="store_true", help="不调模型，只估算调用量与成本")
    ap.add_argument("--limit", type=int, default=0, help="覆盖 L2 的 max_calls")
    args = ap.parse_args()

    run = RUNS / args.run if args.run else _latest_run()
    if not run or not run.exists():
        print("没有可用的运行目录")
        return 1

    cfg = load_sources()
    llm_cfg = cfg.get("llm", {}) or {}
    layers = llm_cfg.get("layers", {}) or {}
    mat_layer = layers.get("material", {}) or {}

    cands = _load_candidates(run)
    mats = _load_materials(run)
    print(f"回放对象：{run.name}｜内容 {len(cands)} 条｜素材 {len(mats)} 张")
    print(f"文档注入：{llm_context.describe()}")

    if args.dry:
        _dry_material(
            cands,
            int(mat_layer.get("max_items_per_query", 40)),
            int(mat_layer.get("min_items", 3)),
            args.limit or int(mat_layer.get("max_calls", 30)),
        )
        picked = llm_adjudicate.conflicts(
            mats,
            extra_noise=[],
            min_evidence=int((layers.get("adjudicate", {}) or {}).get("min_evidence", 3)),
            limit=int((layers.get("adjudicate", {}) or {}).get("max_calls", 25)),
        )
        print(f"\n[dry] L3：{len(picked)} 个「高证据却被判噪声」的矛盾样本（1 次调用）")
        return 0

    llm = probe(llm_cfg)     # 手动回放不占用生产开关：enabled 只管每天的正式跑批
    if not llm.available:
        print(f"\n❌ 模型不可用：{llm.why_unavailable()}")
        print("   （--dry 不需要 key，可以先跑那个看规模）")
        return 2

    keywords = load_keywords()
    blocked = set(keyword_layer(keywords, "锚点层", "主体"))
    for alist in alias_map(keywords).values():
        blocked |= {a for a in alist if a}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = REPLAY / f"{stamp}-{run.name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 回放也要落审计：出问题能翻到原始响应，不然只能重跑
    llm.run_dir = out_dir
    report: dict = {"run": run.name, "model": llm.model, "dry": False}

    pairs = _pairs(cands)
    today = run.name[:8]
    today = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
    # 公平基线：同一批 pairs 先跑一遍纯规则 aggregate。
    # 拿 materials.json 的 65 张去比是不对的 —— 那份还含榜单补卡和 BGM 采样，
    # 而这里的 aggregate 只有抽取结果。
    rule_mats = aggregate([(c, list(hs)) for c, hs in pairs], today=today, lifecycle=None)
    rule_cards = {m.name for m in rule_mats if m.mentions >= 2}

    # ---------------- L2 ----------------
    if args.layer in {"both", "material"}:
        t0 = time.time()
        batch = llm_material.extract_batch(
            llm,
            cands,
            blocked=blocked,
            max_items=int(mat_layer.get("max_items_per_query", 40)),
            min_items=int(mat_layer.get("min_items", 3)),
            max_calls=args.limit or int(mat_layer.get("max_calls", 30)),
        )
        added = llm_material.apply_to_pairs(pairs, batch)
        produced = aggregate(pairs, today=today, lifecycle=None)

        rule_only = {(h.name, h.etype) for _, hs in pairs for h in hs if h.origin != "llm"}
        llm_out = {
            (h.name, h.etype) for _, hs in pairs for h in hs
            if h.origin == "llm" and h.confidence >= 0.5
        }
        after_cards = {m.name for m in produced if m.mentions >= 2}
        gained = sorted(after_cards - rule_cards)
        lost = sorted(rule_cards - after_cards)

        report["material"] = {
            "stats": batch.stats,
            "added_hits": added,
            "llm_unique_hits": len(llm_out),
            "rule_unique_hits": len(rule_only),
            "cards_rule": len(rule_cards),
            "cards_after": len(after_cards),
            "gained": gained,
            "lost": lost,
            "elapsed_s": round(time.time() - t0, 1),
        }
        print(f"\n== L2 结果 ==")
        print(f"调用 {batch.stats.get('calls')} 次（失败 {batch.stats.get('failed')}）"
              f"｜新增命中 {added} 个｜模型独有素材 {len(llm_out)} 个")
        print(f"≥2 条在用的卡：规则 {len(rule_cards)} 张 → 加模型 {len(after_cards)} 张")
        if gained:
            print("模型带过线的：")
            for n in gained[:20]:
                print(f"  + {n}")
        if lost:
            print(f"反而掉线的：{lost}")
        (out_dir / "llm_material.json").write_text(
            json.dumps({"stats": batch.stats, "detail": batch.detail},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------------- L3 ----------------
    # 注意：L3 的判据已改成「高证据 + 被判噪声」的矛盾检测，
    # 它读的是 recomputed 的噪声理由，不读 materials.json 里的 cls ——
    # 所以用修复后的数据跑，结果通常是 0 个（词表已经修好了）。
    # 想复现 11-09 的误判，要用旧词表，见 sandbox 里的单测。
    if args.layer in {"both", "adjudicate"} and mats:
        verdicts = llm_adjudicate.review(
            llm,
            mats,
            extra_noise=[],
            min_evidence=int((layers.get("adjudicate", {}) or {}).get("min_evidence", 3)),
            limit=int((layers.get("adjudicate", {}) or {}).get("max_calls", 25)),
        )
        report["adjudicate"] = {"stats": verdicts.stats, "detail": verdicts.detail}
        print(f"\n== L3 结果 ==")
        print(f"矛盾样本 {verdicts.stats.get('picked', 0)}｜"
              f"keep {len(verdicts.keep)}｜drop {len(verdicts.drop)}")
        for a in verdicts.alerts:
            print(f"  ⚠️ 疑似误伤：{a['name']}（{a['mentions']} 条，{a['noise_reason']}）")

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    llm.save_cache()
    print(f"\n统计 {llm.stats}")
    print(f"产物：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

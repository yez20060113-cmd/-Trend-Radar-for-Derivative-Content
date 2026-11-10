"""查看并处理「待判定的复用短语」。

用法：
    python scripts/review_phrases.py                       # 看最近一轮的待判定短语
    python scripts/review_phrases.py --run 20261106-1437   # 看指定一轮
    python scripts/review_phrases.py --label 影之刃零=title  # 写入判定结果
    python scripts/review_phrases.py --auto                # 让模型批量判（D 标签层）
    python scripts/review_phrases.py --auto --limit 20     # 限制本批条数

标签：
    copy   文案梗（可以被复制的一句话）
    title  作品名/专名（游戏、剧、歌、人名）
    noise  与本领域无关

规则层（src/judge/works.py）会先把「来源文案在讲作品」的短语挡掉，
这个脚本用于处理规则漏掉的、以及复核规则挡得对不对。

--auto 就是把 `HotspotCriteria.md` §5.4 里写好的 D 标签层接上：
结果同样写 data/state/phrase_labels.json，下一轮 top_phrases 读它生效。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "sandbox" / "runs"
LABELS = ROOT / "data" / "state" / "phrase_labels.json"


def _latest_run() -> Path | None:
    dirs = sorted([p for p in RUNS.iterdir() if p.is_dir()], reverse=True)
    return dirs[0] if dirs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="", help="运行目录名，缺省用最新一轮")
    ap.add_argument("--label", default="", help="写入判定，格式 短语=label")
    ap.add_argument("--auto", action="store_true", help="用模型批量判定（D 标签层）")
    ap.add_argument("--limit", type=int, default=0, help="--auto 时本批最多判几条")
    args = ap.parse_args()

    if args.label:
        if "=" not in args.label:
            print("格式应为 短语=label")
            return 2
        phrase, label = args.label.rsplit("=", 1)
        phrase, label = phrase.strip(), label.strip()
        if label not in {"copy", "title", "noise"}:
            print("label 只能是 copy / title / noise")
            return 2
        data = {}
        if LABELS.exists():
            data = json.loads(LABELS.read_text(encoding="utf-8"))
        data[phrase] = label
        LABELS.parent.mkdir(parents=True, exist_ok=True)
        LABELS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已记录：{phrase} → {label}（共 {len(data)} 条标签）")
        return 0

    run = RUNS / args.run if args.run else _latest_run()
    if not run:
        print("没有可用的运行目录")
        return 1
    todo_path = run / "phrases_todo.json"
    if not todo_path.exists():
        print(f"{run.name}：没有待判定短语")
        return 0

    todo = json.loads(todo_path.read_text(encoding="utf-8"))
    print(f"{run.name}：{len(todo)} 个待判定\n")
    for i, item in enumerate(todo, 1):
        print(f"{i}. {item['phrase']}  （{item['videos']} 条 / 语境比 {item.get('works_ratio', 0)}）")
        print(f"   {item['sample'][:90]}")
        print()

    if not args.auto:
        print("判定后执行：python scripts/review_phrases.py --label 短语=title")
        return 0

    # ---- D 标签层：模型批量判定 ----
    sys.path.insert(0, str(ROOT))
    from src.config import load_sources
    from src.llm import phrases as llm_phrases
    from src.llm.client import probe

    llm_cfg = load_sources().get("llm", {}) or {}
    layer = (llm_cfg.get("layers", {}) or {}).get("phrase", {}) or {}
    llm = probe(llm_cfg)     # 手动判定不占用生产开关
    if not llm.available:
        print(f"❌ 模型不可用：{llm.why_unavailable()}")
        print("   人工流程仍然可用：--label 短语=title")
        return 2

    labels, stats = llm_phrases.label(
        llm, todo, limit=args.limit or int(layer.get("max_items", 12))
    )
    llm.save_cache()
    if not labels:
        print("模型没有给出可用判定（失败或输出不合规），人工流程照旧")
        return 1

    data = {}
    if LABELS.exists():
        data = json.loads(LABELS.read_text(encoding="utf-8"))
    data.update(labels)
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    LABELS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"模型判定 {len(labels)} 条 → {LABELS}（共 {len(data)} 条标签）")
    for phrase, lab in labels.items():
        print(f"  {phrase} → {lab}")
    print(f"统计：{stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

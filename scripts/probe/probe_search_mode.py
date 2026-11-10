"""对比两种检索方式：整页 goto vs 页内改词（SPA）。

为什么要这个脚本：
2026-11-09 为了压 502，把「每次 page.goto」换成「在结果页的搜索框改词」。
请求量从 ~300 降到 ~120，但**必须先证明结果没有变差**，否则省下的请求是拿数据质量换的。

对照组很关键：抖音搜索结果本身带随机排序，所以「goto vs 改词」的重合率
必须和「goto vs goto」对比，否则会把固有抖动误判成改词造成的差异。

指标：
1. 条目数、请求数、耗时
2. URL 重合率（和基线比）
3. 重合条目的文案是否逐字一致
4. 时效分布（改词若丢了 publish_time，会看到明显更老的内容）
5. 搜索后的完整 URL（确认过滤参数还在）

用法：
    python scripts/probe_search_mode.py
    python scripts/probe_search_mode.py --keywords "A,B"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

try:  # 直接运行 `python scripts/probe/xxx.py` 时，sys.path[0] 就是本目录
    from _env import strip_proxy_for_localhost
except ImportError:  # 按模块方式运行时的回退
    from scripts.probe._env import strip_proxy_for_localhost

strip_proxy_for_localhost()
  # noqa: E402

from scripts.collect_douyin import Searcher, run_query  # noqa: E402
from scripts.daily_run import ensure_edge  # noqa: E402

DEFAULT = ["瞬移滑镜慢动作挑战", "霸王龙舞步转场挑战"]


def _run(searcher: Searcher, keyword: str, scroll: int) -> tuple[list[dict], int, float, str]:
    counter = {"n": 0}
    searcher.page.on("request", lambda _r: counter.__setitem__("n", counter["n"] + 1))
    t0 = time.time()
    items, err = run_query(searcher, keyword, scroll, 3, None)
    cost = time.time() - t0
    if err:
        print(f"    ! 采集返回错误：{err}")
    return items, counter["n"], cost, searcher.page.url


def _overlap(a: list[dict], b: list[dict]) -> tuple[float, int, int]:
    ua = {i["url"] for i in a}
    ub = {i["url"] for i in b}
    common = ua & ub
    tb = {i["url"]: i["text"] for i in b}
    same = sum(1 for u in common if next(i["text"] for i in a if i["url"] == u) == tb[u])
    return len(common) / max(1, len(ua)), len(common), same


def _ages(items: list[dict]) -> str:
    ages = [i.get("age_days") for i in items]
    known = sorted(a for a in ages if isinstance(a, int))
    unknown = sum(1 for a in ages if a is None)
    over = sum(1 for a in known if a > 7)
    med = known[len(known) // 2] if known else "-"
    return f"≤7天 {len(known) - over}｜>7天 {over}｜未知 {unknown}｜中位 {med}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", default=",".join(DEFAULT))
    ap.add_argument("--scroll", type=int, default=5)
    args = ap.parse_args()
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    proc = ensure_edge(False)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()

            for kw in keywords:
                a = Searcher(ctx, 7, use_spa=False)
                items_a, req_a, cost_a, url_a = _run(a, kw, args.scroll)
                items_a2, req_a2, cost_a2, _ = _run(a, kw, args.scroll)
                a.close()

                b = Searcher(ctx, 7, use_spa=True)
                _run(b, "NEON", args.scroll)          # 先建立搜索会话
                before = b.spa_calls
                items_b, req_b, cost_b, url_b = _run(b, kw, args.scroll)
                used_spa = b.spa_calls > before
                b.close()

                base_rate, base_n, base_same = _overlap(items_a, items_a2)
                spa_rate, spa_n, spa_same = _overlap(items_a, items_b)

                print(f"\n=== {kw} ===")
                print(f"  A 整页#1 : {len(items_a):>3} 条 / {req_a:>4} 请求 / {cost_a:>4.1f}s")
                print(f"  A 整页#2 : {len(items_a2):>3} 条 / {req_a2:>4} 请求 / {cost_a2:>4.1f}s")
                print(f"  B 改词   : {len(items_b):>3} 条 / {req_b:>4} 请求 / {cost_b:>4.1f}s"
                      f"（实际走{'改词' if used_spa else '整页回退'}）")
                print(f"  请求降幅 : {(1 - req_b / max(1, req_a)) * 100:.0f}%")
                print(f"  基线重合 : {base_n}/{len(items_a)} = {base_rate * 100:.0f}%"
                      f"（goto vs goto，文案一致 {base_same}）")
                print(f"  改词重合 : {spa_n}/{len(items_a)} = {spa_rate * 100:.0f}%"
                      f"（文案一致 {spa_same}）")
                print(f"  时效 A   : {_ages(items_a)}")
                print(f"  时效 B   : {_ages(items_b)}")
                print(f"  URL  B   : {url_b[:110]}")
                keep_filter = "publish_time=7" in url_b
                ok = (
                    spa_rate >= base_rate * 0.8
                    and abs(len(items_b) - len(items_a)) <= max(5, len(items_a) * 0.2)
                    and keep_filter
                )
                print(f"  过滤参数 : {'✅ 保留' if keep_filter else '❌ 丢失 publish_time'}")
                print(f"  判定     : {'✅ 与整页等价' if ok else '⚠️ 需复核'}")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            print("\n已结束本次启动的 Edge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

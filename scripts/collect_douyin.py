"""抖音搜索采集（CDP 驱动，零 token）。

执行顺序：
    python scripts/plan_queries.py          # 1) 生成当日检索计划
    python scripts/collect_douyin.py        # 2) 按计划采集（可中断续采）

设计要点：
- 查询来自 `data/input/query_plan.json`，不再由脚本自己轮转
- **优先在结果页改词**（每查询约 77 个请求），整页加载只发生在首个查询与换页后
  （每次 goto 约 300 个请求；502 就是请求总量堆出来的）
- 查询之间随机间隔（默认 8~15 秒，见 config 的 `delay_min/delay_max`）；
  失败指数退避（5s→15s→45s）；**502 单独走 30/90/180 秒**，且不计入空结果熔断
- **每查完一个就落盘**，中断后加 `--resume` 接着跑
- 跨查询按 url 去重统计「新增唯一」，用来判断某个查询值不值得留
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 连 CDP 前必须清掉代理：本机 shell 常带 HTTP_PROXY=127.0.0.1:xxxxx，
# Playwright 请求 http://127.0.0.1:9222/json/version 会被丢给代理，
# 表现成「Unexpected status 502 when connecting to 127.0.0.1:9222」（2026-11-09）。
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")


def strip_proxy_for_localhost() -> list[str]:
    """清掉代理变量，返回被清掉的键名。

    必须在 `sync_playwright()` **之前**调用：Playwright 的 Node 驱动在那一刻
    继承了环境，之后再改 os.environ 对它无效 —— 第一版把清理放在 connect 外层，
    结果仍然是 502。

    本进程只访问 127.0.0.1(CDP) 和国内站点；QQ音乐实测无代理可直连。
    """
    removed = [k for k in PROXY_KEYS if os.environ.pop(k, None) is not None]
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1"
    return removed

from src.collectors.qq_music import fetch as fetch_chart  # noqa: E402
from src.config import (  # noqa: E402
    alias_map,
    keyword_layer,
    load_sources,
    subject_profiles,
)
from src.constants import TYPE_QUERY  # noqa: E402
from src.judge.anchor import context as anchor_context  # noqa: E402
from src.judge.anchor import match_subject  # noqa: E402

KEYWORDS = ROOT / "CoreAssets" / "keywords.json"
OUT = ROOT / "data" / "input" / "douyin_latest.json"
PLAN = ROOT / "data" / "input" / "query_plan.json"

JS_EXTRACT = """
() => {
  const out = [], seen = new Set();
  document.querySelectorAll('a[href*="/video/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!href || text.length < 8 || seen.has(href)) return;
    seen.add(href);
    out.push({ url: href, text });
  });
  return out;
}
"""

DURATION_RE = re.compile(r"^(?:合集\s*)?(\d{1,2}:\d{2})\s+")
COUNT_RE = re.compile(r"^([\d.]+)(万|w|W)?\s+")
AGE_RE = re.compile(r"(\d+)\s*(小时|天|周|个月|月|年)前\s*$")
AUTHOR_RE = re.compile(r"@([^\s@]{1,20})\s*$")

SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fa5-]+")


class Diag:
    """失败现场记录：截图 + URL/标题/正文前 200 字。

    为什么需要：空结果有三种可能（验证码页 / 真空列表 / 未渲染），
    日志里都长一样，只有截图能区分。
    限量：每次采集最多 `max_shots` 张（默认 3），避免诊断本身把磁盘撑爆。
    """

    def __init__(self, root: Path, max_shots: int = 3) -> None:
        self.dir = root / datetime.now().strftime("%Y%m%d-%H%M")
        self.max_shots = max_shots
        self.shots = 0
        self.records: list[dict] = []

    def capture(self, page, keyword: str, err: str) -> None:
        if self.max_shots <= 0 or self.shots >= self.max_shots:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            safe = SAFE_NAME_RE.sub("_", keyword)[:20] or "q"
            name = f"{self.shots + 1:02d}_{safe}.jpg"
            page.screenshot(path=str(self.dir / name), type="jpeg", quality=70)
            info = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "keyword": keyword,
                "error": err,
                "url": page.url,
                "title": page.title(),
                "file": name,
            }
            try:
                info["body_head"] = page.inner_text("body")[:200].replace("\n", " ")
            except Exception:  # noqa: BLE001
                info["body_head"] = ""
            self.records.append(info)
            self.shots += 1
            self._save()
        except Exception:  # noqa: BLE001 - 诊断失败不能影响采集
            pass

    def record_only(self, keyword: str, err: str, note: str = "") -> None:
        """页签已死时截不了图，至少把「哪一条查询、什么错误」记下来。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.records.append(
                {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "keyword": keyword,
                    "error": err,
                    "file": "",
                    "note": note or "页签已关闭，无法截图",
                }
            )
            self._save()
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        try:
            (self.dir / "diag.json").write_text(
                json.dumps({"shots": self.records}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass



class PageLost(RuntimeError):
    """页签/上下文被关闭，但浏览器进程可能还活着。

    2026-11-08 09:11 就是这种情况：采到第 17 个查询时抛
    `TargetClosedError: Page.wait_for_timeout: Target page, context or browser has been closed`，
    而 9222 端口仍然是通的（浏览器没死，是渲染进程/页签死了）。
    """


def _is_page_closed(exc: BaseException) -> bool:
    """判断异常是不是「页签没了」。

    不能只看类型名：Playwright 有时抛 TargetClosedError，有时抛通用 `Error`，
    只有文案能稳定区分。
    """
    text = str(exc)
    return "has been closed" in text or "Target page" in text


def probe_state(browser, ctx) -> str:
    """采集中的连接状态，用于事后判断「断的是会话还是页签」。

    `conn=False`  → CDP 会话被回收（该改「定期重连」）
    `conn=True` 且 `pages=0` → 只是页签被丢弃（该关睡眠标签页 / 保持窗口前台）
    """
    try:
        conn = str(browser.is_connected())
    except Exception:  # noqa: BLE001
        conn = "?"
    try:
        pages = str(len(ctx.pages))
    except Exception:  # noqa: BLE001
        pages = "?"
    return f"conn={conn} pages={pages}"


def _new_page(ctx):
    return ctx.new_page()


# 502 的退避比「空结果」长得多：网关故障通常按分钟恢复，5/15/45 秒等于没等
GATEWAY_BACKOFF = (30, 90, 180)


def _looks_like_gateway(page) -> bool:
    """识别网关错误页（502 Bad Gateway / kngx）。

    为什么单独判：
    - 这类页面没有结果节点，走原逻辑会先白等 15 秒选择器超时，再叠 3 次退避；
      2026-11-09 有 10 分钟就耗在这种等待上
    - 它也不是「没有内容」，不该计入 `--max-empty` 熔断
    """
    try:
        title = (page.title() or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if "502" in title or "Bad Gateway" in title:
        return True
    try:
        head = (page.inner_text("body") or "")[:120]
    except Exception:  # noqa: BLE001
        return False
    return "Bad Gateway" in head


class Searcher:
    """一次采集会话：优先在结果页里改词，只有首次/换页后才整页加载。

    为什么这样：
    一次 `goto` 搜索页约 300 个请求（首屏 387，其中文档+脚本+埋点），
    页内改词只多 77 个（2026-11-09 实测 500~626 vs 77）。
    35 个查询从约 2 万请求降到约 7 千，这才是对付 502 的正解——
    「调慢间隔」只改速率，不改总量。

    正确性优先：改词后必须等到「结果节点被替换」才认，
    拿不到这个信号就退回整页 goto（宁可多花 300 个请求，也不读旧结果）。
    """

    def __init__(self, ctx, days: int, use_spa: bool = True,
                 spa_first: bool = False) -> None:
        self.ctx = ctx
        self.days = days
        self.use_spa = use_spa          # 允许把改词当作「整页被挡时的救急」
        self.spa_first = spa_first      # 实验：优先改词（会丢 publish_time，默认关）
        self.page = _new_page(ctx)
        self.on_results = False      # 当前页签是否已经停在某个搜索结果上
        self.spa_calls = 0
        self.goto_calls = 0
        self.spa_fallbacks = 0
        self.last_mode = "-"

    def url_for(self, keyword: str) -> str:
        return (
            "https://www.douyin.com/search/"
            + urllib.parse.quote(keyword)
            + f"?type=video&publish_time={self.days}"
        )

    def goto_search(self, keyword: str) -> None:
        self.page.goto(self.url_for(keyword), wait_until="domcontentloaded", timeout=45000)
        self.goto_calls += 1
        self.on_results = True
        self.last_mode = "整页"

    def spa_search(self, keyword: str) -> bool:
        """在当前结果页的搜索框里改词。成功（结果确实重渲染）返回 True。"""
        if not self.use_spa or not self.on_results:
            return False
        page = self.page
        try:
            # 先记住当前第一个结果节点：节点被替换 = 列表真的重渲染了。
            # 只比 URL 不够——SPA 改路由是即时的，结果可能还没换。
            page.evaluate(
                """() => { window.__radarPrev = document.querySelector('a[href*="/video/"]') || null; }"""
            )
            box = page.locator('input[data-e2e="searchbar-input"]')
            if box.count() == 0:
                self.spa_fallbacks += 1
                self.on_results = False
                return False
            box.first.fill(keyword)
            box.first.press("Enter")
            page.wait_for_function(
                """(kw) => {
                    let url = location.href;
                    try { url = decodeURIComponent(url); } catch (e) { return false; }
                    if (!url.includes(kw)) return false;
                    const cur = document.querySelector('a[href*="/video/"]');
                    return !cur || cur !== window.__radarPrev;
                }""",
                arg=keyword,
                timeout=20000,
            )
            self.spa_calls += 1
            self.on_results = True
            self.last_mode = "改词"
            # 回到顶部：上一轮滚过 5 屏，位置会保留；不归零会让本轮少滚几屏、
            # 拿到的条数比整页加载少（实测 66 vs 76）。
            try:
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001 - 改词失败不改语义，调用方退回整页
            self.spa_fallbacks += 1
            self.on_results = False
            return False

    def rebuild(self, reason: str) -> None:
        print(f"[换页] {reason}")
        try:
            self.page.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page = self.ctx.new_page()
        except Exception as exc:  # noqa: BLE001
            if _is_page_closed(exc):
                raise PageLost(f"无法新建页签：{reason}") from exc
            raise
        self.on_results = False

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:  # noqa: BLE001
            pass


def parse_likes(raw: str) -> float | None:
    m = COUNT_RE.match(raw)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2):
        value *= 10000
    return value


def parse_age(text: str) -> tuple[int | None, str]:
    m = AGE_RE.search(text)
    if not m:
        return None, text
    n, unit = int(m.group(1)), m.group(2)
    days = {"小时": 0, "天": n, "周": n * 7, "个月": n * 30, "月": n * 30, "年": n * 365}[unit]
    return days, text[: m.start()].strip()


def parse_card(text: str) -> dict | None:
    body = text
    m = DURATION_RE.match(body)
    if m:
        body = body[m.end():]
    likes = parse_likes(body)
    if likes is not None:
        body = body[COUNT_RE.match(body).end():]
    age_days, body = parse_age(body)
    author = ""
    am = AUTHOR_RE.search(body)
    if am:
        author = am.group(1)
        body = body[: am.start()].strip()
    caption = body.strip()
    if not caption:
        return None
    return {"text": caption, "likes": likes, "age_days": age_days, "author": author}


def backup_previous(tag: str = "") -> Path | None:
    """整轮重采前把旧采集文件备份一份。

    2026-11-09 的教训：`--limit 3` 在无 `--resume` 时把 33 个查询覆盖成 3 个，
    数据直接没了。参数已经修了，但「手一抖就毁数据」这类事故值得常备一份快照。
    同一天只留第一份（最早的原始数据最值钱），总共保留最近 5 份。
    """
    if not OUT.exists():
        return None
    bak_dir = OUT.parent / "backup"
    bak_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{tag}" if tag else ""
    bak = bak_dir / f"douyin_latest.{datetime.now():%Y-%m-%d}{suffix}.json"
    if not bak.exists():
        shutil.copy2(OUT, bak)
    olds = sorted(bak_dir.glob("douyin_latest.*.json"))
    for stale in olds[:-5] if len(olds) > 5 else []:
        stale.unlink(missing_ok=True)
    return bak


def save_atomic(payload: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)


def load_plan() -> tuple[list[dict], int]:
    if not PLAN.exists():
        return [], 0
    try:
        data = json.loads(PLAN.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return [], 0
    queries = [q for q in (data.get("queries") or []) if isinstance(q, dict) and q.get("q")]
    scroll = int((data.get("budget") or {}).get("scroll") or 0)
    return queries, scroll


def legacy_queries(cfg: dict) -> list[dict]:
    """没有计划文件时的兜底：主体轮转 + 模板词 + 榜单歌名。"""
    subjects_n = int(cfg.get("subjects", 2))
    templates = str(cfg.get("templates", "转场挑战"))
    from_chart = int(cfg.get("chart_songs", 8))

    kw = json.loads(KEYWORDS.read_text(encoding="utf-8"))
    profiles = subject_profiles(kw)
    subjects = [
        s for s in keyword_layer(kw, "锚点层", "主体")
        if profiles.get(s, {}).get("weight", 1.0) >= 0.3
    ]
    day = datetime.now().timetuple().tm_yday
    picked = ([subjects[(day * subjects_n + i) % len(subjects)] for i in range(subjects_n)]
              if subjects else [])

    out: list[dict] = []
    for i, name in enumerate(picked):
        quals = TYPE_QUERY.get(profiles.get(name, {}).get("type", ""))
        text = f"{name} {quals[i % len(quals)]}" if quals else name
        out.append({"q": text, "type": "主体", "reason": "轮转", "priority": 0.5})
    out += [{"q": t.strip(), "type": "模板", "reason": "配置", "priority": 0.5}
            for t in templates.split(",") if t.strip()]

    if from_chart > 0:
        amap = alias_map(kw)
        judge_cfg = load_sources().get("judge", {}) or {}
        ctx = anchor_context(kw, judge_cfg)
        qcfg = load_sources().get("sources", {}).get("qq_music", {})
        try:
            songs = fetch_chart([str(c) for c in (qcfg.get("charts") or ["27"])],
                                max_rank=int(qcfg.get("max_rank", 30)),
                                qps_ms=int(qcfg.get("qps_ms", 800)))["songs"]
        except Exception:  # noqa: BLE001
            songs = []
        got = 0
        for song in songs:
            artist = "、".join(song.get("artists") or [])
            # 与判定层同一套规则：单字别名 / 3 字英文别名 / 歧义主体都不算命中
            if not match_subject(artist, subjects, amap, **ctx):
                continue
            out.append({"q": song["name"], "type": "歌名", "reason": "榜单", "priority": 1.0})
            got += 1
            if got >= from_chart:
                break
    return out


def _pause(page, seconds: float, keyword: str) -> None:
    """退避等待；页签在这期间死掉要转成 PageLost，不能冒泡成 exit=1。"""
    try:
        page.wait_for_timeout(seconds * 1000)
    except Exception as exc:  # noqa: BLE001
        raise PageLost(f"{keyword}：退避等待时页签已关闭") from exc


def run_query(searcher: Searcher, keyword: str, scroll: int, retries: int,
              diag: "Diag | None" = None,
              select_timeout: float = 15.0) -> tuple[list[dict], str]:
    """执行单个查询。返回 (items, error)。成功时 error 为空串。

    两个与「稳定性」相关的约定：
    - 默认整页 goto（保证 `publish_time` 生效）；`--spa` 时才尝试页内改词（请求少 73%，但丢时间过滤）
    - 502 是一类单独的错误：退避 30/90/180 秒，且不计入「空结果」熔断
    """
    backoff = 5
    gw = 0
    last_err = ""
    for _attempt in range(retries):
        page = searcher.page
        try:
            if searcher.spa_first and searcher.spa_search(keyword):
                pass
            else:
                searcher.goto_search(keyword)

            if _looks_like_gateway(page):
                last_err = "502"
                wait = GATEWAY_BACKOFF[min(gw, len(GATEWAY_BACKOFF) - 1)]
                gw += 1
                searcher.on_results = False      # 错误页不可复用，下次整页重来
                print(f"    ↳ 网关 502，等 {wait}s 后重试（第 {gw} 次）")
                _pause(page, wait, keyword)
                continue

            try:
                page.wait_for_selector('a[href*="/video/"]', timeout=select_timeout * 1000)
            except Exception:  # noqa: BLE001
                page.wait_for_timeout(4000)
            if "登录后即可搜索" in page.inner_text("body"):
                # 这个提示会偶发出现（页面未完全渲染时），不能立刻判死：
                # 先当普通失败重试，全部重试都这样才认定登录失效
                last_err = "NEED_LOGIN"
                _pause(page, backoff, keyword)
                backoff *= 3
                continue
            for _ in range(scroll):
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(2200)
            cards = page.evaluate(JS_EXTRACT)
            rescued = False
            if not cards and searcher.use_spa:
                # 整页被挡时（502 之外的「骨架屏/空壳」），用页内改词再取一次。
                # 改词会丢掉 publish_time，所以下面按 searcher.days 过滤，旧内容不入库。
                if searcher.spa_search(keyword):
                    page.wait_for_timeout(2500)
                    for _ in range(scroll):
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(2200)
                    cards = page.evaluate(JS_EXTRACT)
                    rescued = bool(cards)
                    if rescued:
                        print("    ↳ 整页为空，改词救急拿到结果")
            if not cards:
                last_err = "空结果"
                _pause(page, backoff, keyword)
                backoff *= 3
                continue

            items: list[dict] = []
            seen: set[str] = set()
            for c in cards:
                href = c.get("url", "")
                if href in seen:
                    continue
                seen.add(href)
                parsed = parse_card(c.get("text", ""))
                if not parsed:
                    continue
                parsed["url"] = "https:" + href if href.startswith("//") else href
                items.append(parsed)
            if rescued:
                # 改词结果没有时间过滤，超窗的直接丢掉，避免旧内容污染判定
                before = len(items)
                items = [
                    i for i in items
                    if not isinstance(i.get("age_days"), int) or i["age_days"] <= searcher.days
                ]
                print(f"    ↳ 救急结果 {before} 条 → 过滤超窗后 {len(items)} 条")
            if items:
                return items, ""
            last_err = "解析为空"
        except RuntimeError as exc:
            if str(exc) == "LOGIN_LOST":
                raise
            if _is_page_closed(exc):
                raise PageLost(f"{keyword}：页签已关闭") from exc
            last_err = str(exc)[:60]
        except Exception as exc:  # noqa: BLE001
            if _is_page_closed(exc):
                raise PageLost(f"{keyword}：页签已关闭") from exc
            last_err = str(exc)[:60]
        # 原实现这里没有 try：页签一死，TargetClosedError 直接冒泡出 main()，
        # 整轮以 exit=1 结束，既没重试也没续采（11-08 09:11 就是这么报废的）。
        _pause(page, backoff, keyword)
        backoff *= 3
    if last_err == "NEED_LOGIN":
        if diag:
            diag.capture(searcher.page, keyword, "NEED_LOGIN")
        raise RuntimeError("LOGIN_LOST")
    if diag and last_err:
        diag.capture(searcher.page, keyword, last_err)
    return [], last_err


def main() -> int:
    # 必须在 sync_playwright() 之前：驱动进程会在启动时固定住环境变量
    removed = strip_proxy_for_localhost()
    if removed:
        print(f"[env] 已对 localhost 关闭代理（清掉 {', '.join(removed)}）")

    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="", help="指定检索计划（默认 data/input/query_plan.json）")
    ap.add_argument("--out", default="", help="指定输出文件（默认 data/input/douyin_latest.json）")
    ap.add_argument("--scroll", type=int, default=None, help="每查询滚动次数（覆盖计划）")
    ap.add_argument("--resume", action="store_true", help="跳过已采过的查询")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个查询（调试用）")
    ap.add_argument("--no-delay", action="store_true", help="不做查询间隔（仅调试）")
    ap.add_argument("--max-empty", type=int, default=6,
                    help="连续多少个查询空结果就中止（疑似被限流，默认 6）")
    ap.add_argument("--max-shots", type=int, default=3,
                    help="失败时最多截几张图（默认 3，0 = 不截图）")
    ap.add_argument("--retries", type=int, default=0,
                    help="每查询重试次数（0 = 用配置；风控窗口里建议 1，快速浅扫）")
    ap.add_argument("--select-timeout", type=float, default=15.0,
                    help="等结果卡片出现的秒数（默认 15；快速浅扫可用 8）")
    ap.add_argument("--empty-cooldowns", type=int, default=-1,
                    help="空结果原地冷却次数（-1 用配置；快速浅扫传 0）")
    ap.add_argument("--page-recycle", type=int, default=0,
                    help="每 N 个查询换一次页签（0 = 用配置里的 page_recycle）")
    ap.add_argument("--spa", action="store_true",
                    help="改词优先（请求少 70%%，但会丢 publish_time 过滤，默认关闭；整页被挡时仍会自动用改词救急）")
    args = ap.parse_args()

    # 调试/验证时把产物写到 sandbox，不碰 data/input 的真实数据
    global PLAN, OUT
    if args.plan:
        PLAN = Path(args.plan)
    if args.out:
        OUT = Path(args.out)

    dcfg = load_sources().get("sources", {}).get("douyin_search", {})
    delay_min = float(dcfg.get("delay_min", 8.0))
    delay_max = float(dcfg.get("delay_max", 15.0))
    retries = args.retries or int(dcfg.get("retries", 3))
    select_timeout = float(args.select_timeout)
    days = int(dcfg.get("publish_time", 7))
    page_recycle = args.page_recycle or int(dcfg.get("page_recycle", 12))
    empty_cooldown_s = int(dcfg.get("empty_cooldown_s", 240))
    empty_cooldowns_max = (
        args.empty_cooldowns if args.empty_cooldowns >= 0
        else int(dcfg.get("empty_cooldowns", 2))
    )

    plan, plan_scroll = load_plan()
    if plan:
        all_queries = plan
        scroll = args.scroll or plan_scroll or int(dcfg.get("scroll", 5))
        source = "plan"
    else:
        all_queries = legacy_queries(dcfg)
        scroll = args.scroll or int(dcfg.get("scroll", 2))
        source = "legacy"
    # --limit 只限制「本轮跑几个」，计划名单仍用全量：
    # 否则 --limit + --resume 会把不在前 N 个里的历史记录当成「旧计划」丢掉。
    queries = all_queries[: args.limit] if args.limit else all_queries

    payload = {"collected_at": datetime.now().isoformat(timespec="seconds"),
               "source": source, "queries": [],
               # 计划名单：覆盖度只统计「本轮计划里的查询」，
               # 否则旧计划遗留的查询会被算成进度（11-08 就虚高了 2 条）
               "plan_queries": [q["q"] for q in all_queries],
               # 计划总数：中途因限流/超时中止时，下游才知道「只覆盖了 N/M 个查询」
               "plan_total": len(all_queries)}
    done: set[str] = set()
    # `--limit` 是「本轮只跑 N 个」，不是「只保留 N 个」。
    # 2026-11-09 踩过：`--limit 3` 在无 --resume 时把整个文件覆盖成 3 条，33 个查询全没了。
    # 所以 --limit 也要走「读旧记录」这条路，只是把本轮这 N 个从旧记录里摘掉重采。
    retry_keys: set[str] = set()
    if (args.resume or args.limit) and OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            plan_keys = {q["q"].lower() for q in all_queries}
            before = old.get("queries", [])
            if args.limit and not args.resume:
                retry_keys = {q["q"].lower() for q in queries}
            # 只保留当前计划里的查询：旧计划遗留的条目会污染覆盖度和候选
            payload["queries"] = [
                q for q in before
                if str(q.get("q", "")).lower() in plan_keys
                and str(q.get("q", "")).lower() not in retry_keys
            ]
            dropped = len(before) - len(payload["queries"]) - len(retry_keys)
            if dropped > 0:
                print(f"[resume] 丢弃 {dropped} 条不在当前计划的旧查询")
            if retry_keys:
                print(f"[limit] 保留其余 {len(payload['queries'])} 条旧记录，"
                      f"本轮重采 {len(retry_keys)} 个")
            payload["plan_total"] = max(int(payload["plan_total"]), len(payload["queries"]))
            # 只跳过**成功**的查询：失败项（空结果 / 超时）下次要重试
            done = {
                str(q.get("q", "")).lower()
                for q in payload["queries"]
                if not q.get("error")
            }
            failed = sum(1 for q in payload["queries"] if q.get("error"))
            print(f"[resume] 已有 {len(done)} 个成功查询，跳过；待重试失败 {failed} 个")
        except Exception:  # noqa: BLE001
            pass

    diag = Diag(ROOT / "sandbox" / "diag", max_shots=args.max_shots)
    pending = [q for q in queries if q["q"].lower() not in done]

    # 从零开始的重采：先给旧文件留一份快照，再往里写
    if not payload["queries"] and OUT.exists():
        bak = backup_previous()
        if bak:
            print(f"[backup] 整轮重采，上一版采集已备份：{bak.name}")
    print(f"计划 {len(all_queries)} 个查询（本轮跑 {len(queries)} 个），待采 {len(pending)} 个，"
          f"每查询滚动 {scroll} 次，间隔 {delay_min:.0f}~{delay_max:.0f}s，"
          f"每 {page_recycle} 个查询换页签，"
          f"搜索方式 {'改词优先（实验）' if args.spa else '整页优先（被挡时改词救急）'}")

    seen_urls = {i.get("url") for q in payload["queries"] for i in q.get("items", []) if i.get("url")}
    total = sum(len(q.get("items", [])) for q in payload["queries"])
    started = time.time()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        except Exception as exc:  # noqa: BLE001
            first = str(exc).splitlines()[0][:160]
            print("[错误] 连不上 127.0.0.1:9222 —— Edge 没起来，或调试端口被别的进程占了。")
            print("       先确认：python scripts/launch_edge.py --check")
            print(f"       原始错误：{first}")
            # 退出码 3 交给 daily_run：它会重启 Edge 后用 --resume 续采
            raise SystemExit(3) from None
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # 新建自己的页签：复用 ctx.pages[0] 会关掉用户正在看的页面
        searcher = Searcher(ctx, days, use_spa=True, spa_first=args.spa)

        empty_streak = 0
        gateway_streak = 0
        cooldowns = 0
        empty_cooldowns = 0
        for idx, item in enumerate(pending):
            keyword = item["q"]
            if idx and not args.no_delay:
                time.sleep(random.uniform(delay_min, delay_max))

            # 定期换页签：单页签连开几十个抖音搜索页，DOM 一直累积不释放，
            # 渲染进程很容易崩（11-08 第 17 个查询时崩过一次）。
            if page_recycle and idx and idx % page_recycle == 0:
                try:
                    searcher.rebuild(f"已跑 {idx} 个查询，换新页签")
                except Exception as exc:  # noqa: BLE001
                    print(f"[中止] 无法新建页签：{exc}")
                    save_atomic(payload)
                    return 4

            try:
                t0 = time.time()
                items, err = run_query(
                    searcher, keyword, scroll, retries, diag,
                    select_timeout=select_timeout,
                )
            except PageLost as exc:
                diag.record_only(keyword, "PageLost", str(exc))
                print(f"[恢复] 页签被关闭（{str(exc)[:50]}），准备重建")
                items, err = [], ""
                recovered = False
                # 不能立刻重试：渲染进程刚崩过，Edge 要几秒才缓过来。
                # 11-08 就是因为「重建后马上重跑」再一次失败，然后整轮放弃。
                for attempt in range(1, 4):
                    wait = 5 * attempt                     # 5s → 10s → 15s
                    print(f"[恢复] 等 {wait}s 后重建页签（第 {attempt}/3 次）")
                    time.sleep(wait)
                    try:
                        searcher.rebuild(f"页签丢失，第 {attempt} 次重建")
                        items, err = run_query(
                            searcher, keyword, scroll, retries, diag,
                            select_timeout=select_timeout,
                        )
                        recovered = True
                        break
                    except PageLost as exc2:
                        diag.record_only(keyword, f"PageLost-{attempt}", str(exc2))
                        continue
                    except RuntimeError:
                        print("[中止] 登录态失效，请先在 Edge 里重新登录抖音")
                        save_atomic(payload)
                        return 3
                if not recovered:
                    print("[中止] 重建页签 3 次仍失败，已采部分已保存，可用 --resume 续采")
                    save_atomic(payload)
                    return 4
            except RuntimeError:
                print("[中止] 登录态失效，请先在 Edge 里重新登录抖音")
                save_atomic(payload)
                return 3

            fresh = 0
            for it in items:
                if it.get("url") and it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    fresh += 1
            total += len(items)
            # 重试同一查询时替换旧记录，避免同一 q 出现两条
            payload["queries"] = [
                q for q in payload["queries"]
                if str(q.get("q", "")).lower() != keyword.lower()
            ]
            payload["queries"].append({
                "q": keyword, "type": item.get("type", ""), "publish_time": days,
                "items": items, "new_unique": fresh, "error": err,
                # 观测字段：mode 区分「改词 / 整页」；aged_out 记录超过时间窗的条数。
                # 页内改词不带 publish_time 过滤，这两个数用来事后判断有没有退化。
                "mode": searcher.last_mode,
                "aged_out": sum(
                    1 for i in items
                    if isinstance(i.get("age_days"), int) and i["age_days"] > days
                ) if days else 0,
            })
            save_atomic(payload)
            flag = f"✗ {err}" if err else f"+{fresh} 新"
            cost = time.time() - t0
            print(f"[{idx + 1}/{len(pending)}] {keyword[:26]:<28}{len(items):>4} 条  {flag}"
                  f"  {cost:>5.0f}s {searcher.last_mode} {probe_state(browser, ctx)}")

            # 连续空结果通常不是「真的没内容」，而是被限流/验证码拦住了。
            # 继续跑只会把整个采集阶段（默认 90 分钟）的预算烧光，还会加重风控，所以主动中止。
            # 「解析为空」也算：页面有卡片但解析不出文案，同样是异常状态。
            # 502 **不算**：它是网关故障，按另一条逻辑冷却重试（见下）。
            empty_streak = empty_streak + 1 if err in {"空结果", "解析为空"} else 0
            if args.max_empty and empty_streak >= args.max_empty:
                # 2026-11-09 实测：这类「空结果」多是间歇性软拦截（搜索页停在骨架屏），
                # 一个窗口 3~9 个查询就断，几分钟到十几分钟后自行恢复。
                # 所以先原地冷却再继续，而不是立刻退出——否则整轮只能采到 3~4 个。
                if empty_cooldowns < empty_cooldowns_max:
                    empty_cooldowns += 1
                    print(f"[冷却] 连续 {empty_streak} 个空结果，等 {empty_cooldown_s}s 后继续"
                          f"（第 {empty_cooldowns}/{empty_cooldowns_max} 次）")
                    time.sleep(empty_cooldown_s)
                    empty_streak = 0
                    continue
                print(f"[中止] 连续 {empty_streak} 个查询空结果，冷却 {empty_cooldowns_max} 次仍无改善；"
                      f"已采部分已保存，可用 --resume 稍后续采")
                break

            # 502 是网关错误：连续出现时先冷却一次（实测故障按分钟恢复），
            # 冷却 2 次仍不恢复才中止交给 daily_run 换进程续采。
            if err == "502":
                gateway_streak += 1
                if gateway_streak >= 3:
                    if cooldowns >= 2:
                        print("[中止] 网关持续 502，已采部分已保存，可用 --resume 续采")
                        break
                    cooldowns += 1
                    print(f"[冷却] 连续 {gateway_streak} 个 502，等 180s（第 {cooldowns}/2 次）")
                    time.sleep(180)
                    gateway_streak = 0
            else:
                gateway_streak = 0
                if not err:
                    cooldowns = 0

        searcher.close()

    elapsed = time.time() - started
    ok = sum(1 for q in payload["queries"] if not q.get("error"))
    print(f"\n共 {total} 条｜成功 {ok}/{len(payload['queries'])} 个查询｜用时 {elapsed / 60:.1f} 分钟")
    print(f"搜索方式：改词 {searcher.spa_calls} 次 / 整页 {searcher.goto_calls} 次"
          f"（改词失败回退 {searcher.spa_fallbacks} 次）")
    print(f"写盘：{OUT}")
    if diag.shots:
        print(f"失败现场：{diag.dir}（{diag.shots} 张截图 + diag.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

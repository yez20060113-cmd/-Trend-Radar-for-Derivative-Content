"""豚榜（tunbang.com）热点聚合采集。

它是第三方站，把微博 / 抖音 / 百度 / 知乎等 40 个平台的公开榜单聚合成一张表，
每 30 分钟更新，**服务端渲染**——直接 HTTP + 正则解析，不需要 Playwright。

为什么用它：现有的 `douyin_hot` 只有抖音一个榜，而且是新闻榜，娱乐和玩法词很少。
这个站的「娱乐明星榜」把 24 个平台的娱乐向话题合并成 60 条，是「今天该搜什么」
的高效入口。11-13 实测：微博主榜 50 条里有 15 条娱乐向，其中 6 条是《后会友期》。

它**不替代**原有采集链路，只负责产出「查询词」。抓下来的标题 ≠ 结论，
必须回抖音搜一次看真实使用量。

已知局限：
- 分类页是关键词规则筛的，会误收（科技/财经/游戏）也会漏（综艺 CP 梗）
- 所以同时抓微博主榜，两个来源互补
- 第三方站，可能改版或挂掉 —— 失败时返回空列表，不影响主流程
"""

from __future__ import annotations

import html
import re
from typing import Any

from .http import Fetcher

# 只抓这两个：娱乐榜覆盖广，微博主榜抓综艺/CP 梗（娱乐榜会漏）
DEFAULT_BOARDS: dict[str, str] = {
    "entertainment": "https://tunbang.com/topics/entertainment/",
    "weibo": "https://tunbang.com/hot/weibo/",
}

_ROW_RE = re.compile(r'<li class="row">(.*?)</li>', re.S)
_RANK_RE = re.compile(r'<span class="rank[^"]*">\s*(\d+)\s*</span>')
_TITLE_RE = re.compile(
    r'<a class="row-title" href="([^"]*)"[^>]*>(.*?)</a>', re.S
)
_CHIP_RE = re.compile(r'<span class="chip"><a href="[^"]*">([^<]*)</a></span>')
_HOT_RE = re.compile(r'<span class="hot">([^<]*)</span>')
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    """去标签 + 反转义。标题里有 &amp; 这类实体。"""
    return html.unescape(_TAG_RE.sub("", raw or "")).strip()


def parse_board(page: str, board: str, limit: int = 100) -> list[dict[str, Any]]:
    """解析一个榜单页。结构是 `<li class="row">` + `.rank/.row-title/.chip/.hot`。"""
    out: list[dict[str, Any]] = []
    for block in _ROW_RE.findall(page)[:limit]:
        m_title = _TITLE_RE.search(block)
        if not m_title:
            continue
        title = _text(m_title.group(2))
        if not title:
            continue
        m_rank = _RANK_RE.search(block)
        m_chip = _CHIP_RE.search(block)
        m_hot = _HOT_RE.search(block)
        out.append(
            {
                "title": title,
                "url": m_title.group(1),
                "rank": int(m_rank.group(1)) if m_rank else 0,
                "platform": _text(m_chip.group(1)) if m_chip else "",
                "heat": _text(m_hot.group(1)) if m_hot else "",
                "board": board,
            }
        )
    return out


def collect(
    fetcher: Fetcher,
    cfg: dict[str, Any],
    fetched_at: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """抓多个榜并去重。返回 (条目, 覆盖字段)。

    单个榜失败不抛异常——这只是「找种子」，拿不到就不加查询，不能拖垮跑批。
    """
    boards = cfg.get("boards") or DEFAULT_BOARDS
    limit = int(cfg.get("limit", 100))
    qps = int(cfg.get("qps_ms", 500))

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, url in boards.items():
        try:
            page = fetcher.get_text(url, qps_ms=qps, retries=1)
        except Exception as exc:  # noqa: BLE001 - 第三方站，失败就跳过
            errors.append(f"{name}: {exc}")
            continue
        for row in parse_board(page, name, limit=limit):
            key = row["title"]
            if key in seen:
                continue
            seen.add(key)
            row["fetched_at"] = fetched_at
            items.append(row)

    items.sort(key=lambda x: (-(x["rank"] == 0), x["rank"]))
    return items, ["title", "rank", "platform", "heat"]

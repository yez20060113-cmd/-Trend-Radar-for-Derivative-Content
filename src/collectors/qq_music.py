"""QQ音乐榜单采集 —— BGM 主源。

为什么用音乐平台榜单，而不是抖音的 music 字段：

- 抖音 PC 网页版**不暴露** music 字段。已实测三条路都不通：
  视频页 DOM、分享页 `iesdouyin/share/video`、页面内嵌 `RENDER_DATA`
  （RENDER_DATA 里那个 "music" 是搜索卡片配置，不是视频 BGM）
- 但「哪个艺人发了新歌」在音乐平台是公开数据，服务端渲染，零成本、免登录
- 榜单有排名；⚠️ JSON-LD 里**没有**排名升降（`trend`），所以本模块只返回 `chart` / `rank`，
  不提供升降信息。需要升降只能另找接口（网页可见文本里有，但结构不稳定）

数据来源：页面内嵌的 JSON-LD（schema.org MusicRecording 列表），结构稳定、易解析。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

CHART_URL = "https://y.qq.com/n/ryqq/toplist/{cid}"
CHART_NAMES = {"27": "新歌榜", "26": "热歌榜", "4": "流行指数榜", "62": "飙升榜"}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


def _find_item_list(node: Any) -> list[dict[str, Any]] | None:
    """JSON-LD 是一个 @graph 大对象，榜单挂在其中某个节点上，需要递归找。"""
    if isinstance(node, dict):
        items = node.get("itemListElement")
        if isinstance(items, list) and items:
            return items
        for value in node.values():
            found = _find_item_list(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_item_list(value)
            if found:
                return found
    return None


def _parse_chart(html: str, chart_name: str) -> list[dict[str, Any]]:
    for block in _LDJSON_RE.findall(html):
        try:
            data = json.loads(block)
        except Exception:  # noqa: BLE001
            continue
        items = _find_item_list(data)
        if not items:
            continue
        out: list[dict[str, Any]] = []
        for entry in items:
            item = entry.get("item") or {}
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            artists = [
                str(a.get("name") or "").strip()
                for a in (item.get("byArtist") or [])
                if a.get("name")
            ]
            out.append(
                {
                    "name": name,
                    "artists": artists,
                    "rank": int(entry.get("position") or 0),
                    "url": str(item.get("url") or ""),
                    "chart": chart_name,
                }
            )
        return out
    return []


def fetch(
    chart_ids: list[str],
    *,
    max_rank: int = 30,
    timeout: float = 20.0,
    qps_ms: int = 800,
) -> dict[str, Any]:
    """抓取指定榜单，返回 {songs: [...], charts: [...]}。任一榜单失败不影响其他。

    同名歌曲可能同时出现在多个榜单（NEON 在新歌榜和热歌榜都有），按名字去重。
    """
    songs: list[dict[str, Any]] = []
    charts: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    with httpx.Client(headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True) as cli:
        for index, cid in enumerate(chart_ids):
            cid = str(cid).strip()
            if not cid:
                continue
            if index:
                # 连续请求同一站点会被限流，热歌榜曾因此解析为空
                time.sleep(max(0, qps_ms) / 1000)
            label = CHART_NAMES.get(cid, f"榜单{cid}")
            rows: list[dict[str, Any]] = []
            for attempt in range(2):
                try:
                    resp = cli.get(CHART_URL.format(cid=cid))
                    resp.raise_for_status()
                    rows = _parse_chart(resp.text, label)
                except Exception as exc:  # noqa: BLE001
                    if attempt == 1:
                        errors.append(f"{label}:{exc}"[:80])
                    continue
                if rows:
                    break
                if attempt == 1:
                    errors.append(f"{label}:解析为空")
            if not rows:
                continue
            charts.append(label)
            for row in rows:
                if not 0 < row["rank"] <= max_rank:
                    continue
                key = row["name"].strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                songs.append(row)

    return {"songs": songs, "charts": charts, "errors": errors}

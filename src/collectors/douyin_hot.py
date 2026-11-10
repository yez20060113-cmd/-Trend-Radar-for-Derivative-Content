"""抖音热榜采集。热点条目源，不做任何价值判断。"""

from __future__ import annotations

from typing import Any

from ..models import Candidate
from .http import Fetcher


def collect(fetcher: Fetcher, cfg: dict[str, Any], fetched_at: str) -> tuple[list[Candidate], list[str]]:
    url = cfg["url"]
    limit = int(cfg.get("limit", 60))
    qps = int(cfg.get("qps_ms", 0))

    data = fetcher.get_json(url, qps_ms=qps, retries=1)
    items = data.get("list") or []
    if not items:
        raise ValueError("抖音热榜返回空列表")

    out: list[Candidate] = []
    for idx, item in enumerate(items[:limit], start=1):
        title = (item.get("title") or "").strip()
        if not title:
            continue
        field: dict[str, Any] = {}
        if item.get("hot_value") is not None:
            field["hot_value"] = item["hot_value"]
        extra = item.get("extra") or {}
        if isinstance(extra, dict) and extra.get("video_count") is not None:
            field["video_count"] = extra["video_count"]
        if item.get("view_count") is not None:
            field["view_count"] = item["view_count"]

        out.append(
            Candidate(
                term=title,
                platform="douyin",
                source="douyin_hot",
                rank=int(item.get("index") or idx),
                field=field,
                fetched_at=fetched_at,
            )
        )

    return out, sorted({"term", "rank", "hot_value", "video_count"} & {k for c in out for k in c.field})

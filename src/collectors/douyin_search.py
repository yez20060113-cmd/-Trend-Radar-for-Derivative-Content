"""抖音搜索结果读取。

采集由 Agent 用浏览器完成（见 CoreAssets/PlatformSources.md），
落成 JSON 后由本模块转成候选对象。脚本不直接驱动浏览器。

输入格式：
{
  "collected_at": "2026-11-04T17:30:00",
  "queries": [
    {"q": "A2O LITE", "publish_time": 7, "items": [
      {"url": "https://www.douyin.com/video/123", "text": "文案 #标签 #标签2",
       "likes": 74000, "age_days": 3, "author": "橙子o🍊"}
    ]}
  ]
}
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Candidate

HASHTAG_RE = re.compile(r"#([^\s#]{1,20})")


def _parse_tags(text: str) -> list[str]:
    return [t.strip() for t in HASHTAG_RE.findall(text or "") if t.strip()]


def load(path: Path, *, now: datetime) -> tuple[list[Candidate], dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少抖音采集输入：{path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload.get("queries") or []
    out: list[Candidate] = []
    seen: set[str] = set()

    for q in queries:
        keyword = str(q.get("q", ""))
        for idx, item in enumerate(q.get("items") or [], start=1):
            text = str(item.get("text") or "").strip()
            url = str(item.get("url") or "")
            if not text:
                continue
            key = url or text
            if key in seen:
                continue
            seen.add(key)

            age_days = item.get("age_days")
            field: dict[str, Any] = {}
            if isinstance(item.get("likes"), (int, float)):
                field["likes"] = float(item["likes"])
            if isinstance(age_days, (int, float)):
                field["age_days"] = float(age_days)
                field["pubdate"] = time.time() - float(age_days) * 86400

            out.append(
                Candidate(
                    term=text,
                    author=str(item.get("author") or ""),
                    platform="douyin",
                    source="douyin_search",
                    url=url or None,
                    rank=idx,
                    tags=_parse_tags(text)[:12],
                    field=field,
                    combo=keyword,
                    fetched_at=now.isoformat(timespec="seconds"),
                )
            )

    # 查询成败要单独记账：只覆盖 15/40 个查询时，日报必须自己说明数据不完整。
    # 覆盖度只统计「本轮计划里的查询」——旧计划遗留的查询不算进度
    # （11-08 就因为把它们算进去，把 33/40 显示成了 35/40）。
    plan_names = {str(x).lower() for x in (payload.get("plan_queries") or [])}
    attempted = len(queries)
    failed_attempts = sum(1 for q in queries if q.get("error"))
    if plan_names:
        planned = len(plan_names)
        ok_q = sum(
            1 for q in queries
            if not q.get("error") and str(q.get("q", "")).lower() in plan_names
        )
    else:
        planned = int(payload.get("plan_total") or 0) or attempted
        ok_q = attempted - failed_attempts
    meta = {
        "collected_at": payload.get("collected_at", ""),
        "queries": [str(q.get("q", "")) for q in queries],
        "items": len(out),
        "queries_planned": planned,
        "queries_attempted": attempted,
        "queries_failed": max(0, planned - ok_q),
        "queries_ok": ok_q,
    }
    return out, meta

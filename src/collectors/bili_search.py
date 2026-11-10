"""B站组合搜索采集：验证源，不做热点发现。

核心规则：B站空格搜索不是严格 AND，必须在结果侧做「主体命中校验」，
否则会混入古风/街舞/游戏音乐等噪声。
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..models import Candidate
from .http import Fetcher

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or "")).strip()


def _build_combos(
    subjects: list[str],
    creation_types: list[str],
    scenes: list[str],
    max_combos: int,
    offset: int = 0,
) -> list[tuple[str, str]]:
    kinds = (creation_types[:2] + scenes[:2]) or creation_types or scenes
    if not subjects or not kinds:
        return []
    combos: list[tuple[str, str]] = []
    for i in range(max_combos):
        subj = subjects[(offset + i) % len(subjects)]
        kind = kinds[i % len(kinds)]
        pair = (subj, kind)
        if pair not in combos:
            combos.append(pair)
    return combos


def _hit_subject(text: str, subject: str, aliases: dict[str, list[str]]) -> bool:
    if not text:
        return False
    pool = [subject, *aliases.get(subject, [])]
    low = text.lower()
    return any(p.lower() in low for p in pool if p)


def collect(
    fetcher: Fetcher,
    cfg: dict[str, Any],
    fetched_at: str,
    *,
    subjects: list[str],
    aliases: dict[str, list[str]],
    creation_types: list[str],
    scenes: list[str],
    offset: int = 0,
) -> tuple[list[Candidate], list[str], str]:
    """返回 (候选列表, 可用字段, 备注)。"""
    combos = _build_combos(
        subjects, creation_types, scenes, int(cfg.get("max_combos", 6)), offset
    )
    if not combos:
        raise ValueError("没有可用的搜索组合（主体或类型词为空）")

    top_n = int(cfg.get("top_n", 5))
    qps = int(cfg.get("qps_ms", 320))
    referer = cfg.get("referer", "https://www.bilibili.com")

    seen: dict[str, Candidate] = {}
    failed: list[str] = []

    for subject, kind in combos:
        label = f"{subject} {kind}"
        try:
            data = fetcher.get_json(
                cfg["search_url"],
                params={"search_type": "video", "keyword": label, "page": 1},
                referer=referer,
                qps_ms=qps,
            )
            groups = (data.get("data") or {}).get("result") or []
            videos: list[dict[str, Any]] = []
            for group in groups:
                if group.get("result_type") == "video":
                    videos = group.get("data") or []
                    break
        except Exception:  # noqa: BLE001
            failed.append(f"{label}(搜索失败)")
            continue

        videos.sort(key=lambda v: v.get("play") or 0, reverse=True)
        for video in videos[:top_n]:
            bvid = video.get("bvid") or ""
            if not bvid:
                continue
            title = _clean(video.get("title", ""))
            try:
                tag_data = fetcher.get_json(
                    cfg["tag_url"], params={"bvid": bvid}, referer=referer, qps_ms=qps
                )
                tags = [t.get("tag_name", "") for t in (tag_data.get("data") or []) if t.get("tag_name")]
            except Exception:  # noqa: BLE001
                tags = []

            if not (_hit_subject(title, subject, aliases) or any(_hit_subject(t, subject, aliases) for t in tags)):
                continue  # 主体命中校验不通过，丢弃

            if bvid in seen:
                if subject not in seen[bvid].anchors:
                    seen[bvid].anchors.append(subject)
                continue

            field: dict[str, Any] = {}
            for key in ("play", "like", "review", "favorites", "danmaku", "pubdate"):
                if video.get(key) is not None:
                    field[key] = video[key]

            seen[bvid] = Candidate(
                term=title,
                platform="bilibili",
                source="bili_search",
                url=f"https://www.bilibili.com/video/{bvid}",
                tags=tags[:12],
                field=field,
                anchors=[subject],
                combo=label,
                fetched_at=fetched_at,
            )

    note = f"{len(combos)} 组合"
    if failed:
        note += f"；失败 {len(failed)} 个"
    fields = sorted({k for c in seen.values() for k in c.field})
    return list(seen.values()), fields, note

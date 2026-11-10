"""BGM 采样器：给「素材」补配乐，而不是给每条视频补配乐。

为什么这样设计：
- 抖音网页版不暴露 music 字段（视频页 / 分享页 / RENDER_DATA / type=music / 汽水音乐 全部实测排除）
- 唯一能拿到真实 BGM 的是第三方接口，按次计费，免费额度 100 次/天
- 创作者要的是「这个挑战 / 这段文案配什么歌」，不是「这条视频配什么歌」

三层省钱逻辑（按顺序判断，命中即跳过）：
1. 素材本身就是 BGM（歌）→ 不用再抽
2. 文案里已出现同一首歌 → 直接推断，不花配额
3. 已经抽过（缓存，默认 7 天有效）→ 复用

剩下的才真正调用接口，并受 daily_budget 约束。

⚠️ 占位结果不入日报：
「模板音乐 / 模板创作人」「@某某创作的原声」是剪映模板 / 原声的通用占位，
不是可检索的歌（实测两个不同挑战会返回**同一个 music_id**）。
这类结果只写缓存（带 placeholder 标记），避免重复花配额，但不覆盖素材的配乐字段。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..config import env
from ..models import Candidate, Material
from ..normalize.songmatch import song_in_text

AWEME_RE = re.compile(r"/video/(\d{6,})")
MEDIA_SKIP = ["图文", "合集", "直播回放", "直播切片", "照片"]

# 剪映模板 / 原声的通用占位名，对创作者没有参考价值
PLACEHOLDER_RE = re.compile(
    r"(模板音乐|模板创作人|创作的原声|剪映模板|^原声$|^bgm$)", re.I
)


def is_placeholder(title: str) -> bool:
    t = (title or "").strip()
    return not t or bool(PLACEHOLDER_RE.search(t))


@dataclass
class Plan:
    tasks: list[dict] = field(default_factory=list)      # {material, aweme_id, reason}
    skipped: dict[str, int] = field(default_factory=dict)
    inferred: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def aweme_id(url: str | None) -> str:
    m = AWEME_RE.search(url or "")
    return m.group(1) if m else ""


def load_cache(path: Path, ttl_days: float = 7) -> dict[str, Any]:
    """读缓存，并丢掉过期条目。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict) or ttl_days <= 0:
        return data if isinstance(data, dict) else {}

    cutoff = datetime.now() - timedelta(days=ttl_days)
    fresh: dict[str, Any] = {}
    for name, rec in data.items():
        if not isinstance(rec, dict):
            continue
        stamp = str(rec.get("fetched_at") or "")
        try:
            if stamp and datetime.fromisoformat(stamp) < cutoff:
                continue
        except ValueError:
            pass
        fresh[name] = rec
    return fresh


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _infer_from_text(
    cands: list[Candidate], songs: list[dict], ratio: float
) -> tuple[str, str]:
    """文案里反复出现同一首歌 → 直接认定它是配乐，不花配额。"""
    if not cands or not songs:
        return "", ""
    best, best_hit = "", 0
    for song in songs:
        hit = sum(1 for c in cands if song_in_text(f"{c.term} {' '.join(c.tags)}", song))
        if hit > best_hit:
            best, best_hit = str(song.get("name") or ""), hit
    if not best:
        return "", ""
    # 样本少时（1~2 条），命中 1 条就足以推断；样本多时按比例
    if best_hit / len(cands) >= ratio or (len(cands) <= 2 and best_hit >= 1):
        return best, "inferred"
    return "", ""


def plan(
    materials: list[Material],
    cands_by_name: dict[str, list[Candidate]],
    *,
    chart_songs: list[dict],
    cache: dict[str, Any],
    daily_budget: int,
    per_material: int = 1,
    infer_ratio: float = 0.6,
) -> Plan:
    """决定这一轮要抽哪些视频的 BGM。不改动 materials，只产出计划。

    推断结果会直接写回 materials（bgm_title / bgm_source=inferred）。
    """
    by_norm = {str(s.get("name") or "").lower(): s for s in chart_songs if s.get("name")}
    result = Plan()

    for mat in materials:
        if mat.cls != "热点":
            continue
        if mat.mtype == "BGM":
            result.skip("本身是歌")
            continue

        cands = cands_by_name.get(mat.name, [])
        if not cands:
            result.skip("无非抖音来源")
            continue

        # 1a) 素材名本身含歌名（「DESERVE翻唱挑战」）→ 直接推断
        name_hit = ""
        for song in chart_songs:
            if song_in_text(mat.name, song):
                name_hit = str(song.get("name") or "")
                break

        # 1b) 文案已自曝歌名 → 推断
        title, how = _infer_from_text(cands, chart_songs, infer_ratio)
        if name_hit:
            title, how = name_hit, "inferred"
        if title:
            song = by_norm.get(title.lower(), {})
            mat.bgm_title = title
            mat.bgm_author = "、".join(song.get("artists") or [])
            mat.bgm_source = how
            result.inferred += 1
            result.skip("文案已含歌名")
            continue

        # 2) 缓存命中（占位结果也命中缓存，但不再展示）
        if mat.name in cache:
            cached = cache[mat.name]
            if cached.get("placeholder") or is_placeholder(str(cached.get("title") or "")):
                result.skip("曾采到占位/原声")
                continue
            mat.bgm_title = str(cached.get("title") or "")
            mat.bgm_author = str(cached.get("author") or "")
            mat.bgm_source = "api"
            result.skip("已缓存")
            continue

        # 3) 挑代表视频：互动最高的、且不是图文合集
        pool = [c for c in cands if aweme_id(c.url) and not any(w in c.term for w in MEDIA_SKIP)]
        if not pool:
            result.skip("无可用视频 ID")
            continue
        pool.sort(key=lambda c: float(c.field.get("likes") or 0), reverse=True)

        for cand in pool[:per_material]:
            if len(result.tasks) >= daily_budget:
                result.skip("超出预算")
                break
            result.tasks.append(
                {"material": mat.name, "aweme_id": aweme_id(cand.url), "likes": cand.field.get("likes") or 0}
            )

    return result


def fetch_one(aweme_id_: str, *, base_url: str, endpoint: str, timeout: float) -> dict[str, Any] | None:
    """调用 SandBase 拿单条视频详情，返回 {title, author, music_id, user_count}。"""
    key = env("SANDBASE_API_KEY")
    if not key:
        return None
    url = base_url.rstrip("/") + endpoint
    try:
        resp = httpx.post(
            url,
            json={"aweme_id": aweme_id_},
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None

    music = _find_music(data)
    if not music:
        return None
    title = str(music.get("title") or "")
    return {
        "title": title,
        "author": str(music.get("author") or ""),
        "music_id": str(music.get("mid") or music.get("id_str") or music.get("id") or ""),
        "user_count": int(music.get("user_count") or music.get("listen_count") or 0),
        "is_original": bool(music.get("is_original_sound") or False),
        "placeholder": is_placeholder(title),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def _find_music(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """接口返回结构可能有多层包装，递归找 music 对象。"""
    if depth > 6:
        return None
    if isinstance(node, dict):
        music = node.get("music")
        if isinstance(music, dict) and (music.get("title") or music.get("mid")):
            return music
        for value in node.values():
            found = _find_music(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:5]:
            found = _find_music(value, depth + 1)
            if found:
                return found
    return None


def enrich(
    materials: list[Material],
    tasks: list[dict],
    *,
    base_url: str,
    endpoint: str,
    timeout: float,
    cache: dict[str, Any],
) -> dict[str, int]:
    """执行采样，把结果写回 materials 和 cache。没有 key 时只做空转。"""
    stats = {"calls": 0, "ok": 0, "failed": 0, "placeholder": 0}
    if not env("SANDBASE_API_KEY"):
        return stats

    by_name = {m.name: m for m in materials}
    for task in tasks:
        stats["calls"] += 1
        info = fetch_one(task["aweme_id"], base_url=base_url, endpoint=endpoint, timeout=timeout)
        mat = by_name.get(task["material"])
        if not info or not info.get("title"):
            stats["failed"] += 1
            continue
        # 占位/原声：写缓存防止重复花配额，但不覆盖素材的配乐字段
        cache[str(task["material"])] = info
        if info.get("placeholder") or info.get("is_original"):
            stats["placeholder"] += 1
            continue
        stats["ok"] += 1
        if mat is not None and not mat.bgm_title:
            mat.bgm_title = info["title"]
            mat.bgm_author = info["author"]
            mat.bgm_source = "api"
    return stats

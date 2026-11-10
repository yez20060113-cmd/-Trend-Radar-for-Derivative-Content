"""从一条内容里抽取「素材实体」：歌 / 模板 / 挑战。

判定单位不是视频，而是素材。所以第一步必须把「视频标题」翻译成「用它做了什么素材」。
"""

from __future__ import annotations

import re

from ..models import Candidate, EntityHit
from .pattern import STOP_WORDS as GENERIC_NAMES

QUOTE_RE = re.compile(r"[《「“]([^》」”]{1,30})[》」”]")

# 出现这些词，说明书名号/引号里的东西是「歌」
# 注意：「教程」「模板」「剪同款」属于**模板/玩法语境**，不是音乐语境。
# 它们曾导致「霸王龙舞步制作教程」把标签里的 #cortis 当成歌名，已移除。
SONG_COOCCUR = [
    "出道曲", "新歌", "主打", "回归曲", "单曲", "专辑", "原声", "bgm", "配乐",
    "翻唱", "翻跳", "卡点", "音源", "ost", "主题曲", "片尾曲", "片头曲",
    "帮唱", "练习室", "原唱", "歌曲", "收录", "remix",
]

# 出现这些词，说明引号里是作品名（剧/综艺/电影），不是素材
WORK_COOCCUR = ["剧", "电影", "综艺", "首播", "定档", "大结局", "剧集", "短剧", "纪录片"]

STOP_NAMES = {
    "舞台", "直拍", "合集", "完整版", "现场", "演唱会", "练习室", "片段", "花絮",
    "封面", "预告", "mv", "bgm", "原声", "纯享", "高清", "4k", "8k",
}

EN_STOP = {
    "bgm", "mv", "live", "hd", "vip", "a2o", "lite", "tf", "sm", "yg", "ai",
    "dj", "ost", "amv", "mad", "focus", "kpop", "kpop", "ft", "feat", "remix",
    "intro", "outro", "ver", "ver.", "the", "and", "you",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,19}")
_CHALLENGE_RE = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9]{2,16}(挑战|挑战赛)$")


def _clean_name(raw: str) -> str:
    name = raw.strip().strip("《》「」“”""''·.。，, ")
    name = re.sub(r"(完整版|官方版|高音质|无损|原版|翻自|cover)$", "", name, flags=re.I).strip()
    return name


def _valid_name(name: str) -> bool:
    if not name or len(name) > 30:
        return False
    if name.lower() in STOP_NAMES:
        return False
    if name.isdigit():
        return False
    return True


def extract_songs(c: Candidate) -> list[EntityHit]:
    text = f"{c.term} {' '.join(c.tags)}"
    low = text.lower()
    has_song_ctx = any(w in low for w in SONG_COOCCUR)
    has_work_ctx = any(w in low for w in WORK_COOCCUR)

    hits: list[EntityHit] = []
    seen: set[str] = set()

    for m in QUOTE_RE.finditer(text):
        name = _clean_name(m.group(1))
        if not _valid_name(name) or name.lower() in seen:
            continue
        # 作品名（剧/综艺）不是素材，除非同时有强音乐语境
        if has_work_ctx and not has_song_ctx:
            continue
        confidence = 0.85 if has_song_ctx else (0.6 if not _CJK_RE.search(name) else 0.45)
        seen.add(name.lower())
        hits.append(
            EntityHit(
                name=name, etype="BGM", confidence=confidence,
            )
        )

    # 英文曲名兜底已移除：
    # 搜索结果里的英文 token 绝大多数是团名（#cortis）或账号名（@alekskost），
    # 靠「上下文里有音乐词」判断会大面积误判。宁可漏，不要错。
    return hits


def extract_templates(c: Candidate) -> list[EntityHit]:
    hits: list[EntityHit] = []
    is_challenge = bool(_CHALLENGE_RE.match(c.term.strip()))
    for item in c.template_hits:
        name, family = (item.split("@", 1) + [""])[:2]
        if not name:
            continue
        # 整条标题已经是挑战名时，不再拆出更碎的模板片段
        if is_challenge and family in {"挑战", "转场挑战"}:
            continue
        if name.lower() in STOP_NAMES or name in GENERIC_NAMES:
            continue
        hits.append(
            EntityHit(
                name=name, etype="模板",
                confidence=0.8 if family in {"变装", "换装", "转场", "转场挑战", "手势舞", "仿妆"} else 0.6,
            )
        )
    return hits


def extract_challenges(c: Candidate) -> list[EntityHit]:
    term = c.term.strip()
    if _CHALLENGE_RE.match(term) and 4 <= len(term) <= 20:
        return [EntityHit(name=term, etype="挑战", confidence=0.8)]
    return []


def extract(c: Candidate) -> list[EntityHit]:
    """抽出一条内容里所有可用的素材实体，按名字去重。"""
    hits = extract_challenges(c) + extract_songs(c) + extract_templates(c)
    out: list[EntityHit] = []
    seen: set[tuple[str, str]] = set()
    for h in hits:
        key = (h.name.lower(), h.etype)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out

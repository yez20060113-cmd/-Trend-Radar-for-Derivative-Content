"""歌名匹配：判断一段文案里是否真的提到了某一首歌。

规则来自两次踩坑：
- `With`（RESCENE）靠普通英文单词匹配到一堆无关文案 → 非全大写英文名必须带 #话题或同现歌手
- `慢倍速` 是 B站标题里的描述语 → 短中文名同样要求 #话题或同现歌手

主流程和 BGM 采样器共用这一份规则，避免两处判断不一致。
"""

from __future__ import annotations

import re
from typing import Any


def song_in_text(text: str, song: dict[str, Any]) -> bool:
    name = str(song.get("name") or "").strip()
    if len(name) < 3:
        return False
    low = (text or "").lower()
    key = name.lower()
    artists = [a for a in (song.get("artists") or []) if len(a) >= 2]

    def _tagged() -> bool:
        return f"#{key}" in low or any(a.lower() in low for a in artists)

    if key.isascii():
        if not re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", low):
            return False
        # 全大写（NEON / DESERVE）视为专名，允许裸匹配；
        # 首字母大写或全小写（With / stay）撞普通词，必须带话题或歌手
        if name.isupper() or len(name) > 12:
            return True
        return _tagged()

    if len(name) <= 4:
        # 短名先要求「歌名本身出现」。
        # 2026-11-09 的教训：原来直接 return _tagged()，而「同现歌手」这一支
        # 并不要求歌名出现 —— `刘宇` ⊂ `刘宇宁`，于是 13 条刘宇宁视频
        # 全被算成刘宇的《春日序曲》，还把这张卡判成了「圈内」。
        if key not in low:
            return False
        return _tagged()
    return key in low

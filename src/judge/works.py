"""作品语境词：判断一段文案是不是在讲「某个作品」（游戏/影视/动漫/综艺）。

为什么需要它：
「影之刃零」曾在复用短语层被判成文案梗，实际是游戏名。短语层只数频次，
不知道这是什么——所以补一层「上下文在讲什么」的规则判断。

注意：这里判的是**文案的语境**，不是文案本身。命中越多，说明这段文字越像
作品介绍/攻略/剧集讨论，而不是「一句话被抄来抄去」。
"""

from __future__ import annotations

WORK_CUES: dict[str, list[str]] = {
    "游戏": [
        "副本", "boss", "攻略", "通关", "抽卡", "装备", "皮肤", "赛季", "开黑",
        "主线", "支线", "章节", "全收集", "成就", "cg", "手游", "端游", "steam",
        "ps5", "switch", "联机", "存档", "读档", "版本更新", "内测", "公测",
        "试玩", "手柄", "建模", "打击感", "数值", "氪金",
    ],
    "影视": [
        "剧集", "预告", "花絮", "首播", "定档", "大结局", "第几集", "cut",
        "片花", "路透", "杀青", "开播", "追剧", "剧透", "番外", "番位",
        "票房", "排片", "口碑", "影评",
    ],
    "动漫": [
        "番剧", "新番", "第几话", "声优", "op", "ed", "剧场版", "漫画",
        "连载", "原作", "漫改", "轻小说", "同人志",
    ],
    "综艺": [
        "正片", "加更", "会员版", "花絮", "嘉宾", "赛制", "淘汰赛", "舞台纯享",
    ],
}

# 有人称 / 语气 / 动作标记的，更像「一句话」而不是「一个作品名」
SENTENCE_MARKERS = [
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "嘛",
    "不", "很", "太", "就", "都", "还", "又", "才", "再", "也",
]

# 命中比达到这个比例，就认为这批文案在讲作品，而不是在复制一句话
WORK_RATIO_THRESHOLD = 0.6


def _hits(low: str) -> list[str]:
    out: list[str] = []
    for words in WORK_CUES.values():
        out.extend(w for w in words if w and w in low)
    return out


def works_ratio(texts: list[str]) -> float:
    """给定一段短语的来源文案，返回命中作品语境的比例。"""
    if not texts:
        return 0.0
    hit = sum(1 for t in texts if _hits((t or "").lower()))
    return hit / len(texts)


def looks_like_title(phrase: str, texts: list[str]) -> bool:
    """短语是否更像「作品名/专名」而不是「可复制的句子」。

    两个条件同时成立才判专名：
    1. 来源文案里 ≥60% 命中作品语境词
    2. 短语本身不含人称/语气/动作标记（否则它本身就像一句话）
    """
    if any(marker in phrase for marker in SENTENCE_MARKERS):
        return False
    return works_ratio(texts) >= WORK_RATIO_THRESHOLD

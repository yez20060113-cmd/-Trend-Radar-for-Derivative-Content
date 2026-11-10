"""模板骨架抽取：把「XX摇 / XX变装 / XX转场挑战」这类构词规则变成可执行的识别器。

为什么不用词表：模板词是开集，每年新增几百个，人工永远追不上。
骨架只描述"长什么样"，具体词由系统从文本里抽。

两条路径，标签优先：

1. **话题标签优先**：`#霸王龙舞步转场挑战#` 的边界天然完整，整段就是模板名。
2. **正文回溯兜底**：没有标签时，从骨架往前回溯最多 8 字，再做品牌 / 口语前缀清洗。

为什么不能只用正则：正则只能「从骨架往回数固定字数」，「霸王龙舞步转场挑战」里
「霸王龙舞步」是 5 个字，超过上限就会被截成「王龙舞步转场挑战」——这正是此前的 bug。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 撞普通词的实例 + 大类概括词（大类不是具名模板，不能当素材）
STOP_WORDS = {
    # 撞普通词
    "摇滚", "摇号", "摇头", "摇奖", "摇摆", "摇一摇",
    "舞狮", "舞龙", "舞台", "舞伴",
    "挑战极限", "挑战自我", "特效药", "滤镜感",
    # 大类概括词：用户明确指出这类不该输出
    "变装", "换装", "转场", "卡点", "混剪", "仿妆",
    "变装挑战", "转场挑战", "变装转场", "反差变装", "反向变装", "古风变装",
    "情侣换装", "抬手变装", "旅行转场", "剧情演绎", "对口型",
}

# 品牌名：经常被粘在模板名前面（「特步走路变装」）
BRAND_WORDS = (
    "特步", "耐克", "阿迪达斯", "阿迪", "李宁", "安踏", "鸿星尔克", "361度", "匹克",
    "彪马", "匡威", "优衣库", "巴黎世家", "古驰", "迪奥", "香奈儿", "圣罗兰",
    "花西子", "完美日记", "橘朵", "珂拉琪", "京东", "淘宝", "拼多多", "抖音",
)

# 口语前缀：从这些词之后才是模板名本体（「卢昱晓也来饭撒摇了」→「饭撒摇」）
PREFIX_STOPS = (
    "也来", "都来", "一起", "快来", "又来", "开始", "跟着", "我也来", "我也",
    "也在", "都在", "这就来", "这就", "教你", "学一下", "解锁", "万能",
    "热门", "超火", "很火", "最近",
)

MAX_TAG_PREFIX = 14   # 标签内前缀上限：标签边界可信，可以放宽
MAX_TEXT_PREFIX = 8   # 正文回溯上限
MAX_WORD_PREFIX = 8   # 正文兜底时前缀最多保留几个字（太短会把「霸王龙舞步」切成「王龙舞步」）

HASHTAG_RE = re.compile(r"#([^\s#]{1,30})")


def _is_name_char(ch: str) -> bool:
    return ch.isalnum() or "\u4e00" <= ch <= "\u9fa5"


@dataclass(frozen=True)
class TemplateHit:
    word: str
    family: str
    start: int = 0
    end: int = 0
    from_tag: bool = False


class PatternMatcher:
    def __init__(
        self,
        skeletons: list[str],
        context_words: list[str],
        self_validating: list[str],
    ) -> None:
        self.families = sorted(
            {s.split("{X}", 1)[1] for s in skeletons if "{X}" in s and s.split("{X}", 1)[1]},
            key=len,
            reverse=True,
        )
        alt = "|".join(re.escape(f) for f in self.families)
        self._fam_re = re.compile(alt) if alt else None
        self.context_words = list(context_words)
        self.self_validating = set(self_validating)

    # ---------- 对外 ----------

    def extract(self, text: str) -> list[TemplateHit]:
        if not self._fam_re or not text:
            return []

        raw: list[TemplateHit] = []
        covered: list[tuple[int, int]] = []

        # 1) 话题标签优先：整段标签就是模板名
        for m in HASHTAG_RE.finditer(text):
            tag = m.group(1).strip()
            fam = self._suffix_family(tag)
            if not fam:
                continue
            prefix = tag[: len(tag) - len(fam)]
            if not 2 <= len(prefix) <= MAX_TAG_PREFIX:
                continue
            word = self._trim(prefix, MAX_TAG_PREFIX) + fam
            if not self._acceptable(word):
                continue
            raw.append(
                TemplateHit(word=word, family=fam, start=m.start(1), end=m.end(1), from_tag=True)
            )
            covered.append((m.start(1), m.end(1)))

        # 2) 正文回溯兜底：跳过已被标签覆盖的区间
        for m in self._fam_re.finditer(text):
            s, e = m.start(), m.end()
            if any(not (e <= cs or s >= ce) for cs, ce in covered):
                continue
            j, n = s, 0
            while j > 0 and n < MAX_TEXT_PREFIX and _is_name_char(text[j - 1]):
                j -= 1
                n += 1
            prefix = self._trim(text[j:s], MAX_WORD_PREFIX)
            if len(prefix) < 2:
                continue
            word = prefix + m.group(0)
            if not self._acceptable(word):
                continue
            raw.append(TemplateHit(word=word, family=m.group(0), start=j, end=e))

        # 3) 去重叠：同一片段优先保留更长的骨架（「转场挑战」优于「挑战」）
        raw.sort(key=lambda h: h.end - h.start, reverse=True)
        kept: list[TemplateHit] = []
        spans: list[tuple[int, int]] = []
        for h in raw:
            if any(not (h.end <= s or h.start >= e) for s, e in spans):
                continue
            spans.append((h.start, h.end))
            kept.append(h)
        kept.sort(key=lambda h: h.start)
        return kept

    def accept(
        self,
        text: str,
        hits: list[TemplateHit],
        *,
        compact_source: bool = False,
    ) -> list[TemplateHit]:
        """候选之后的校验：避免「挑战极限」这类误召回。

        compact_source=True 表示来源本身是榜单条目（已是人工/算法筛过的热点条目），
        噪声率低，直接放行。
        标签里抽出来的（from_tag）自带边界，视为已校验。
        """
        if not hits:
            return []
        if compact_source:
            return hits
        text = text or ""
        if any(w in text for w in self.context_words):
            return hits
        return [h for h in hits if h.family in self.self_validating or h.from_tag]

    # ---------- 内部 ----------

    def _suffix_family(self, tag: str) -> str:
        for fam in self.families:          # 已按长度降序，优先最长骨架
            if fam and tag.endswith(fam) and len(tag) > len(fam):
                return fam
        return ""

    @staticmethod
    def _acceptable(word: str) -> bool:
        if not word or not 3 <= len(word) <= 20:
            return False
        if word in STOP_WORDS or word.isdigit():
            return False
        return True

    @staticmethod
    def _trim(prefix: str, cap: int) -> str:
        """去掉品牌名与口语前缀，再从右截取 cap 个字。"""
        s = prefix
        for b in BRAND_WORDS:
            if s.startswith(b):
                s = s[len(b):]
                break
        for w in PREFIX_STOPS:
            if w in s:
                s = s.rsplit(w, 1)[-1]
                break
        for b in BRAND_WORDS:
            if s.startswith(b):
                s = s[len(b):]
                break
        return s[-cap:] if len(s) > cap else s

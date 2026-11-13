"""模板词生命周期：记录每个抽取词的首次出现时间，用来自动衰减热度。

模板词不像主体，它有过期问题。靠人记「手势舞现在不好用了」不可持续，
所以这里只存事实（first_seen / last_seen / 次数），权重由判定层按年龄算。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

# 实体抽取写入的「伪族名」：它们不是骨架，不该被骨架下线清理逻辑删掉
# phrase 是复用短语（2026-11-11 加）：同样不属于任何骨架族，
# 不保护的话每轮都会被 prune 当孤儿删掉，first_seen 天天重置，永远算「新梗」
TYPE_FAMILIES = {"BGM", "模板", "挑战", "phrase"}

# 短语归一化的门槛：最长公共子串 ≥4 字，且占较长那个词的 ≥60%
# 「你就这样闯进我」vs「就这样闯进我的心窝」→ LCS「就这样闯进」5 字，5/8=0.63 → 合并
# 「我要逮捕你」vs「你被逮捕了」→ LCS「逮捕」2 字，太短 → 不合并（避免误伤）
_SIMILAR_MIN_COMMON = 4
_SIMILAR_THRESHOLD = 0.6


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度。短语都很短（<20 字），O(n·m) 够用。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, cj in enumerate(b, 1):
            if ch == cj:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


class Lifecycle:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 状态文件损坏时从空开始，不阻塞主流程
                self.data = {}

    def seed(self, word: str, family: str, first_seen: str) -> None:
        """用词库里的人工标注（YYYY-MM）预置首见时间，避免老模板被当成新词。"""
        if word in self.data:
            return
        self.data[word] = {
            "first_seen": first_seen,
            "last_seen": first_seen,
            "count": 0,
            "family": family,
        }

    def observe(self, word: str, family: str, today: str) -> None:
        rec = self.data.get(word)
        if not rec:
            self.data[word] = {
                "first_seen": today,
                "last_seen": today,
                "count": 1,
                "family": family,
            }
            return
        rec["last_seen"] = today
        rec["count"] = int(rec.get("count", 0)) + 1
        rec.setdefault("family", family)

    def age_days(self, word: str, today: str) -> int | None:
        rec = self.data.get(word)
        if not rec or not rec.get("first_seen"):
            return None
        try:
            first = date.fromisoformat(rec["first_seen"])
            now = date.fromisoformat(today)
        except ValueError:
            return None
        return max(0, (now - first).days)

    def find_similar(
        self,
        word: str,
        family: str,
        *,
        min_common: int = _SIMILAR_MIN_COMMON,
        threshold: float = _SIMILAR_THRESHOLD,
    ) -> tuple[str | None, dict | None]:
        """在同类记录里找同一个梗的其他写法，返回 (代表词, 记录)；找不到返回 (None, None)。

        为什么需要：短语是 n-gram 切出来的，同一句话在不同内容里长度不同，
        会产生「你就这样闯进我」「就这样闯进我的心窝」这类碎片。
        不归一化的话每轮都被当成新词，`first_seen` 天天重置，
        「常青老梗」永远不会触发（2026-11-13 发现）。

        取「首见最早」的那个，而不是相似度最高的——今天刚登记的碎片
        跟查询词完全一样（ratio=1.0），按相似度排会命中它自己，年龄永远是 0。
        """
        best: tuple[str, str, dict] | None = None   # (first_seen, 词, 记录)
        for key, rec in self.data.items():
            if rec.get("family") != family:
                continue
            common = _lcs_len(word, key)
            if common < min_common:
                continue
            ratio = common / max(len(word), len(key))
            if ratio < threshold:
                continue
            first = str(rec.get("first_seen") or "")
            if best is None or first < best[0]:
                best = (first, key, rec)
        if not best:
            return None, None
        return best[1], best[2]

    @staticmethod
    def weight(age: int | None) -> int:
        """距首次出现的时效权重：新词才值钱。"""
        if age is None:
            return 3
        if age <= 7:
            return 3
        if age <= 30:
            return 2
        if age <= 60:
            return 1
        return 0

    def prune(self, valid_families: set[str], keep: set[str]) -> int:
        """清掉骨架已下线后遗留的孤儿记录，避免状态文件越积越脏。

        ⚠️ `family` 有**两种来源**，不能一视同仁：

        - 骨架族名（`转场挑战` / `变装` / `摇`…）：由 `PatternMatcher` 写入，
          骨架从词库下线后才该清理
        - 素材类型（`模板` / `挑战` / `BGM`）：由实体抽取写入，这些词本来就不属于
          任何骨架族，如果按骨架族判定，它们会在下一轮被当成孤儿删掉，
          `first_seen` 每天重置 → 词龄权重永远是「新词」
        """
        drop = [
            word for word, rec in self.data.items()
            if rec.get("family") not in valid_families
            and rec.get("family") not in TYPE_FAMILIES
            and word not in keep
        ]
        for word in drop:
            del self.data[word]
        return len(drop)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

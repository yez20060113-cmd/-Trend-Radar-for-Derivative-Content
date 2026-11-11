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

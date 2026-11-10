"""把已有文档切成可注入的片段。

为什么不整篇塞：`HotspotCriteria.md` 421 行 ≈ 12k token。三层加起来每天约 46 次
调用，整篇注入会把成本放大 5 倍，而且大半内容跟当前这一层无关。

做法：按 `##` / `###` 标题切块，每一层只取自己需要的章节。取不到的章节返回空串，
prompt 里就少一段 —— 宁可少给上下文，也不让缺文件把整轮搞挂。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import CORE_DIR

_HEAD_RE = re.compile(r"^(#{2,3})\s+(.+)$")
MAX_CHARS = 6000

# 每层需要的章节。键是标题里的特征词，命中即取。
LAYER_SECTIONS: dict[str, list[str]] = {
    # L1 查询生成：给「不同主体找的东西不一样」和检索计划的分层
    "plan": ["三层标签", "类型化检索", "检索计划"],
    # L2 素材理解：给抽取规则、合并规则、圈层门槛
    "material": ["素材抽取", "同一玩法先合并", "先分圈层"],
    # L3 裁决：给过滤门槛和判定原则
    "adjudicate": ["过滤与门槛", "判定原则"],
    # D 标签层：复用短语的两道过滤
    "phrase": ["复用短语"],
}

_cache: dict[str, list[tuple[str, str]]] = {}


def _split(doc: Path) -> list[tuple[str, str]]:
    """切成 [(标题, 正文)]。

    只取标题**直属**的正文，不把子标题内容并进来 —— 否则父节和子节同时命中时
    同一段文字会被注入两遍。本项目的父节（`## 四、素材卡判定`）都有引言句，
    不存在「选到父节却是空壳」的情况。
    """
    key = str(doc)
    if key in _cache:
        return _cache[key]
    if not doc.exists():
        _cache[key] = []
        return []

    lines = doc.read_text(encoding="utf-8").splitlines()
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        m = _HEAD_RE.match(line)
        if m:
            blocks.append((m.group(2).strip(), []))
        elif blocks:
            blocks[-1][1].append(line)

    out = [(title, "\n".join(body).strip()) for title, body in blocks]
    _cache[key] = out
    return out


def pick(doc_name: str, layer: str, *, max_chars: int = MAX_CHARS) -> str:
    """取某一层要用的章节，拼成一段 markdown。"""
    sections = _split(CORE_DIR / doc_name)
    if not sections:
        return ""
    keys = LAYER_SECTIONS.get(layer, [])
    picked: list[str] = []
    for title, body in sections:
        if not body:
            continue
        if any(k in title for k in keys):
            picked.append(f"## {title}\n{body}")
    if not picked:
        return ""
    text = "\n\n".join(picked)
    if len(text) > max_chars:
        # 截断时保住标题，避免模型拿到半句话
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n…（以下省略）"
    return text


def for_layer(layer: str, docs: list[str] | None = None) -> str:
    """按层取全部文档片段。docs 缺省用 HotspotCriteria。"""
    names = docs or ["HotspotCriteria.md"]
    parts = [pick(name, layer) for name in names]
    return "\n\n".join(p for p in parts if p)


def describe() -> dict[str, int]:
    """各层注入的字符数，用来在日志里核对「文档真的进去了」。"""
    return {layer: len(for_layer(layer)) for layer in LAYER_SECTIONS}

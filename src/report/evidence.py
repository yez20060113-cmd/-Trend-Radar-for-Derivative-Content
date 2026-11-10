"""素材卡证据文案：日报、邮件、摘要共用一份。

原来「N 条内容在用」这句在 `render.py` 和 `email_html._proof` 各写了一遍，
措辞已经出现差异（「条内容在用」vs「条在用」）。统一到这里，只保留一个版本，
`short=True` 给邮件用（横向空间小）。
"""

from __future__ import annotations

_LONG_UNIT = {"BGM": "条内容提到该歌名", "模板": "条内容在用", "挑战": "条内容在用"}
_SHORT_UNIT = {"BGM": "条提到歌名", "模板": "条在用", "挑战": "条在用"}


def count_phrase(
    mtype: str,
    mentions: int,
    anchor_mentions: int = 0,
    circle: str = "",
    *,
    short: bool = False,
) -> str:
    """「30 条内容在用（含主体 29 条）」/「46 条提到歌名」。

    只有圈内、且确实有主体命中时才带括号——跨圈卡片的主体数不是有效分母。
    """
    units = _SHORT_UNIT if short else _LONG_UNIT
    text = f"{int(mentions)} {units.get(mtype, '条在用')}"
    if circle == "圈内" and int(anchor_mentions or 0):
        text += f"（含主体 {int(anchor_mentions)}）"
    return text

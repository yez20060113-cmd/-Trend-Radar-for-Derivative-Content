"""素材卡判定：素材够不够格进日报。

两条线，门槛不同：

- **圈内**（引用它的内容里出现过你关注的主体）：这是「追星二创」的主战场，门槛低
- **跨圈**（无锚点）：别人圈里的模板，可以套用，但门槛必须高，否则会把通用挑战灌满日报

BGM 另有一条特殊规则：来自音乐榜单、且歌手是你关注的主体 → 即使抖音上暂无使用记录也输出，
因为「某艺人发新歌」本身就是可行动信息（拿来做视频自带流量）。
"""

from __future__ import annotations

from ..models import Material
from ..normalize.term import normalize as _norm
from ..store.lifecycle import Lifecycle
from .domain import DOMAIN_NOISE, HARD_NOISE_GROUPS, hit, hits as _hits

CIRCLE_IN = "圈内"
CIRCLE_OUT = "跨圈"

# 「零使用也输出」只适用于真正的新歌榜 / 飙升榜；
# 热歌榜是存量热度，一首 2003 年的老歌不该按「新歌」进日报。
NEW_CHARTS = ("新歌榜", "飙升榜")


def _chart_is_new(mat: Material) -> bool:
    return any(k in (mat.chart or "") for k in NEW_CHARTS)


def _noise_reason(
    name: str, samples: list[str], extra_noise: list[str] | None = None
) -> str:
    """判断一条素材是否属于「与二创无关」的领域。

    两条 2026-11-09 定下来的规则（都是被误判逼出来的）：

    1. **单条样本不足以判死**。硬领域（体育/电竞）要求「素材名命中」或「≥2 条样本命中」。
       原来是把名称 + 3 条样本拼成一段文本、任意一个词命中就丢弃，
       结果《SUPERSTAR》因为一条文案里的「被李秀满淘汰」被判成体育领域。
    2. **多义词进软领域**。`淘汰/晋级/逆转/夺冠/教练/战队/小组赛/八强/四强`
       在选秀、综艺、游戏里太常见，放在 `赛事` 组，需要 ≥2 个词同现才算噪声。
    """
    name_low = (name or "").lower()
    sample_lows = [(s or "").lower() for s in (samples or [])]

    def sample_hits(word: str) -> int:
        return sum(1 for s in sample_lows if hit(s, word))

    hard_reason = ""
    soft: list[tuple[str, list[str]]] = []

    for group, words in DOMAIN_NOISE.items():
        in_name = _hits(name_low, words)
        multi = [w for w in words if sample_hits(w) >= 2]
        if group in HARD_NOISE_GROUPS:
            if in_name:
                hard_reason = f"{group}领域（{'/'.join(in_name[:2])}）"
                break
            if multi:
                hard_reason = f"{group}领域（{'/'.join(multi[:2])}）"
                break
            continue
        # 软领域：任意位置命中 ≥2 个不同词
        merged = list(dict.fromkeys([*in_name, *[w for w in words if sample_hits(w) >= 1]]))
        if len(merged) >= 2:
            soft.append((group, merged))

    if hard_reason:
        return hard_reason

    if extra_noise:
        in_name = _hits(name_low, extra_noise)
        multi = [w for w in extra_noise if sample_hits(w) >= 2]
        if in_name or multi:
            return f"黑名单（{'/'.join((in_name or multi)[:2])}）"

    if soft:
        return f"{soft[0][0]}领域（{'/'.join(soft[0][1][:3])}）"
    return ""


def _key(mat: Material) -> str:
    """模型裁决的键：归一名 + 类型。改名/换类型就不算同一条。"""
    return f"{_norm(mat.name)}|{mat.mtype}"


def judge_materials(
    materials: list[Material],
    *,
    lifecycle: Lifecycle | None,
    today: str,
    recent_days: int = 7,
    max_output: int = 8,
    per_anchor_cap: int = 2,
    max_cross: int = 3,
    extra_noise: list[str] | None = None,
    recent_hot: dict[str, dict] | None = None,
    max_repeat: int = 3,
    repeat_growth: float = 0.2,
    llm_verdicts: dict[str, str] | None = None,
) -> list[Material]:
    """`llm_verdicts` 是模型复核结果（键见 `_key`）：只有 keep / drop 两种。

    - `keep`：免除噪声判定，让高证据素材不被词表误伤
    - `drop`：模型确认它确实属于无关领域，直接判死

    只影响「噪声判定」这一处，不改排序、不改门槛、不改配额。
    """
    verdicts = llm_verdicts or {}
    for mat in materials:
        vkey = _key(mat)
        verdict = verdicts.get(vkey, "")

        # 模型主张丢弃：直接判死，理由带上规则原来的判断
        if verdict == "drop":
            mat.cls = "丢弃"
            mat.llm_verdict = "drop"
            mat.reason = f"模型复核：与追星二创无关（原判 {mat.reason or '候选'}）"
            continue

        # 榜单 + 圈内的歌不参与噪声判定：它是「你关注的人发了什么新歌」，
        # 不该因为某条抖音文案里出现「淘汰」「电竞」这类词被丢掉（2026-11-09 SUPERSTAR）。
        exempt = (mat.from_chart and mat.circle == CIRCLE_IN) or verdict == "keep"
        noise = "" if exempt else _noise_reason(mat.name, mat.samples, extra_noise)
        if noise:
            mat.cls = "丢弃"
            mat.reason = noise
            continue
        if verdict == "keep":
            mat.llm_verdict = "keep"

        mat.circle = mat.circle or CIRCLE_OUT
        age = lifecycle.age_days(mat.name, today) if lifecycle else None
        fresh = age is not None and age <= recent_days
        age_text = f"，首见{age}天前" if age is not None else ""
        need = 2 if mat.circle == CIRCLE_IN else 3

        # 规则 1：圈内艺人的榜单新歌 —— 没有抖音使用记录也输出
        if (
            mat.mtype == "BGM"
            and mat.from_chart
            and mat.circle == CIRCLE_IN
            and _chart_is_new(mat)
        ):
            used = (
                f"；抖音 {mat.mentions} 条内容提到该歌名"
                if mat.mentions
                else "；抖音暂无相关内容"
            )
            mat.cls = "热点"
            mat.reason = (
                f"{mat.chart or '榜单'} #{mat.chart_rank} · "
                f"{mat.artist or '—'}{used}"
            )

        # 规则 2：有人在用（圈内门槛低，跨圈门槛高）
        elif mat.mentions >= need:
            mat.cls = "热点"
            unit = "提到该歌名" if mat.mtype == "BGM" else "在用"
            anchor_note = (
                f"（含主体 {mat.anchor_mentions} 条）"
                if mat.circle == CIRCLE_IN and mat.anchor_mentions
                else ""
            )
            mat.reason = f"{mat.circle}；{mat.mentions} 条内容{unit}{anchor_note}{age_text}"
        # 规则 3：单条 + 新 + 来自榜单 —— 仅限圈内
        elif mat.mentions == 1 and fresh and mat.from_board and mat.circle == CIRCLE_IN:
            mat.cls = "热点"
            mat.reason = f"圈内；1 条内容在用，来自榜单{age_text}"
        else:
            mat.cls = "候选"
            mat.reason = f"{mat.circle}；{mat.mentions} 条在用，未达 {need} 条门槛{age_text}"

        # 榜单新歌的加成：只补偿「冷启动」，不能压过真的有人在用的素材
        # 否则「新歌榜 #16、抖音 0 条」会把「10 条在用的挑战」挤掉
        chart_bonus = 0.0
        if mat.from_chart:
            chart_bonus = 3.0 if 0 < mat.chart_rank <= 10 else 1.0

        # 词龄权重（新词更值钱）：0~3 分，只影响排序，不参与门槛
        freshness = lifecycle.weight(age) if lifecycle else 0

        mat.score = (
            mat.mentions * 4
            + max(0, len(mat.platforms) - 1) * 2
            + (3 if fresh else 0)
            + (4 if mat.circle == CIRCLE_IN else 0)
            + chart_bonus
            + freshness
            + min(3.0, mat.signals.get("play_sum", 0) / 100000)
        )

        # ---- 跨天复用：连续第 3 天且热度没涨，就让它让位 ----
        info = (recent_hot or {}).get(_norm(mat.name))
        if mat.cls == "热点" and info:
            streak = int(info.get("streak") or 0)
            last_mentions = int(info.get("last_mentions") or 0)
            last_rank = int(info.get("last_rank") or 0)
            growth = (mat.mentions - last_mentions) / max(1, last_mentions)
            rank_up = bool(
                mat.from_chart and last_rank and mat.chart_rank
                and mat.chart_rank < last_rank
            )
            mat.repeat_days = streak + 1
            # 榜单歌不按「计数涨没涨」判断衰退：覆盖度波动会让 mentions 假性下跌
            # （今天 25/33 个查询，NEON 从 46 掉到 26，但它在榜仍是 #3）。
            # 它们的复用只受「单日复用额度」约束。
            if mat.from_chart:
                mat.repeat = True
                mat.reason = f"{mat.reason}；连续第 {streak + 1} 天（仍在榜）"
            elif streak >= 2 and growth < repeat_growth:
                mat.cls = "候选"
                mat.reason = (
                    f"连续第 {streak + 1} 天出现，热度未增长"
                    f"（{last_mentions} → {mat.mentions} 条），让位给新素材"
                )
            else:
                mat.repeat = True
                note = "（热度上升）" if growth >= repeat_growth else ""
                mat.reason = f"{mat.reason}；连续第 {streak + 1} 天{note}"

    kept: list[Material] = []
    repeats_taken = 0
    for circle, cap in ((CIRCLE_IN, max_output), (CIRCLE_OUT, max_cross)):
        pool = [m for m in materials if m.cls == "热点" and m.circle == circle]
        pool.sort(key=lambda m: (-m.score, m.name))
        counts: dict[str, int] = {}
        taken = 0
        for mat in pool:
            # 配额按「关注主体」算，不按展示用的歌手串：
            # BGM 卡的 artist 是「A2O LITE、A2O」这种拼接，同团两首歌会拿到两个不同的键。
            key = (mat.anchors[0] if mat.anchors else "") or mat.artist or f"__{mat.name}"
            if counts.get(key, 0) >= per_anchor_cap:
                mat.cls = "候选"
                mat.reason = f"{mat.reason}；同一主体已达 {per_anchor_cap} 条上限"
                continue
            if taken >= cap:
                mat.cls = "候选"
                mat.reason = f"{mat.reason}；超出单日输出上限"
                continue
            # 复用卡单日额度：保证每天都有一批新面孔
            if mat.repeat and repeats_taken >= max_repeat:
                mat.cls = "候选"
                mat.reason = f"{mat.reason}；今日复用额度已满（{max_repeat} 张）"
                continue
            counts[key] = counts.get(key, 0) + 1
            if mat.repeat:
                repeats_taken += 1
            mat.order = len(kept)
            taken += 1
            kept.append(mat)

    return materials

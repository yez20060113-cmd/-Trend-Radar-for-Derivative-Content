"""追星二创素材雷达 · 命令行入口。

用法：
    python -m src.main --run                # 跑一次，产物写 sandbox/runs/<时间戳>/
    python -m src.main --run --prod         # 额外产出本轮 daily.json（由 daily_run 发信后归档）
    python -m src.main --prune              # 分层清理（只删重文件，保留日报）
    python -m src.main --clean --days 7     # 清理 7 天前的运行目录
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .collectors import bgm_sampler, bili_search, douyin_hot, douyin_search, qq_music
from .collectors.http import Fetcher
from .config import (
    DATA_DIR,
    ROOT,
    SANDBOX_DIR,
    alias_map,
    keyword_layer,
    load_keywords,
    load_sources,
    pattern_config,
    subject_profiles,
    top_list,
)
from .judge.anchor import context as anchor_ctx
from .judge.anchor import match_subject as _match_subject
from .judge.history import load_recent_hot
from .judge.material import NEW_CHARTS, judge_materials
from .judge.threshold import Threshold, apply as apply_threshold
from .llm import adjudicate as llm_adjudicate
from .llm import context as llm_context
from .llm import from_config as llm_from_config
from .llm import material as llm_material
from .llm import phrases as llm_phrases
from .models import Candidate, EntityHit, Material, SourceStatus
from .normalize import entity
from .normalize.aggregate import aggregate, merge_variants
from .normalize.cluster import cluster_captions
from .normalize.songmatch import song_in_text
from .normalize.phrase import top_phrases
from .normalize.term import normalize as _norm
from .normalize.merge import dedupe
from .normalize.pattern import PatternMatcher
from .report.render import render_report
from .store.lifecycle import Lifecycle
from .store.rundir import clean_old, new_run_dir, now_local, prune, write_json, write_text

log = logging.getLogger("radar")


def _setup_logging(log_file: Path | None) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)


def _collect(
    fetcher: Fetcher, cfg: dict[str, Any], keywords: dict[str, Any], fetched_at: str
) -> tuple[list[Candidate], list[SourceStatus]]:
    sources = cfg.get("sources", {})
    subjects = keyword_layer(keywords, "锚点层", "主体")
    aliases = alias_map(keywords)
    creation_types = keyword_layer(keywords, "形式层", "创作类型词")
    scenes = keyword_layer(keywords, "形式层", "场景词")

    # 检索池只用单主体；CP 名本质是两个艺人，进搜索会污染结果，只用于判定命中
    # C 档（大众情怀型）默认不检索，只在热榜/榜单命中时才用
    profiles = subject_profiles(keywords)
    pool: list[str] = [s for s in subjects if profiles.get(s, {}).get("weight", 1.0) >= 0.3]
    max_combos = int(sources.get("bili_search", {}).get("max_combos", 8))
    offset = 0
    if pool:
        try:
            day_of_year = datetime.fromisoformat(fetched_at).timetuple().tm_yday
        except ValueError:
            day_of_year = 0
        offset = (day_of_year * max_combos) % len(pool)

    candidates: list[Candidate] = []
    statuses: list[SourceStatus] = []

    # 1) 抖音热榜
    hot_cfg = sources.get("douyin_hot", {})
    if hot_cfg.get("enabled"):
        t0 = time.time()
        try:
            items, fields = douyin_hot.collect(fetcher, hot_cfg, fetched_at)
            candidates.extend(items)
            statuses.append(
                SourceStatus(
                    id="douyin_hot", platform="douyin", status="ok", count=len(items),
                    fields=fields, elapsed_ms=int((time.time() - t0) * 1000),
                )
            )
            log.info("抖音热榜：%d 条", len(items))
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(
                    id="douyin_hot", platform="douyin", status="failed",
                    elapsed_ms=int((time.time() - t0) * 1000), note=str(exc)[:80],
                )
            )
            log.warning("抖音热榜失败：%s", exc)
    else:
        statuses.append(SourceStatus(id="douyin_hot", platform="douyin", status="skipped", note="配置关闭"))

    # 1.5) 抖音搜索（Agent 用浏览器采集后写入 JSON）
    ds_cfg = sources.get("douyin_search", {})
    if ds_cfg.get("enabled"):
        t0 = time.time()
        path = ROOT / str(ds_cfg.get("input", "data/input/douyin_latest.json"))
        try:
            items, meta = douyin_search.load(path, now=datetime.fromisoformat(fetched_at))
            th = cfg.get("threshold", {})
            kept, stats = apply_threshold(
                items,
                Threshold(
                    min_likes=float(th.get("min_likes", 500)),
                    max_age_days=float(th.get("max_age_days", 7)),
                    percentile=float(th.get("percentile", 0.4)),
                ),
            )
            candidates.extend(kept)
            planned = int(meta.get("queries_planned") or 0)
            ok_q = int(meta.get("queries_ok") or 0)
            failed_q = int(meta.get("queries_failed") or 0)
            covered = f"{ok_q}/{planned} 个查询成功" if planned else f"{ok_q} 个查询"
            statuses.append(
                SourceStatus(
                    id="douyin_search", platform="douyin",
                    status="ok" if ok_q else "failed",
                    count=len(kept),
                    fields=["likes", "age_days"],
                    elapsed_ms=int((time.time() - t0) * 1000),
                    note=(
                        f"{covered}；过门槛 {stats['kept']}/{stats['input']} "
                        f"(超时{stats['drop_age']} 低赞{stats['drop_likes']} 分位{stats['drop_percentile']})"
                    ),
                    detail={
                        "queries_planned": planned,
                        "queries_ok": ok_q,
                        "queries_failed": failed_q,
                        "items_in": stats["input"],
                        "items_kept": stats["kept"],
                    },
                )
            )
            if planned and ok_q < planned:
                log.warning("抖音搜索只覆盖 %d/%d 个查询，本轮数据不完整", ok_q, planned)
            log.info("抖音搜索：%d 条 → 过门槛 %d 条", stats["input"], len(kept))
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(
                    id="douyin_search", platform="douyin", status="failed",
                    elapsed_ms=int((time.time() - t0) * 1000), note=str(exc)[:80],
                )
            )
            log.warning("抖音搜索读取失败：%s", exc)

    # 2) B站组合搜索
    bili_cfg = sources.get("bili_search", {})
    if bili_cfg.get("enabled"):
        t0 = time.time()
        try:
            items, fields, note = bili_search.collect(
                fetcher, bili_cfg, fetched_at,
                subjects=pool, aliases=aliases,
                creation_types=creation_types, scenes=scenes,
                offset=offset,
            )
            candidates.extend(items)
            statuses.append(
                SourceStatus(
                    id="bili_search", platform="bilibili", status="ok", count=len(items),
                    fields=fields, elapsed_ms=int((time.time() - t0) * 1000), note=note,
                )
            )
            log.info("B站搜索：%d 条（%s）", len(items), note)
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(
                    id="bili_search", platform="bilibili", status="failed",
                    elapsed_ms=int((time.time() - t0) * 1000), note=str(exc)[:80],
                )
            )
            log.warning("B站搜索失败：%s", exc)
    else:
        statuses.append(SourceStatus(id="bili_search", platform="bilibili", status="skipped", note="配置关闭"))

    # 3) 抖音热歌榜（未接入）
    music_cfg = sources.get("douyin_music", {})
    if music_cfg.get("enabled"):
        statuses.append(
            SourceStatus(id="douyin_music", platform="douyin", status="failed", note="采集器尚未实现")
        )
    else:
        statuses.append(
            SourceStatus(
                id="douyin_music", platform="douyin", status="skipped",
                note="未接入；BGM 证据不可用（≠ 无 BGM 证据）",
            )
        )

    return candidates, statuses


def _subject_context(keywords: dict[str, Any], judge_cfg: dict[str, Any]) -> dict[str, Any]:
    """`_match_subject` 的固定参数：画像、成员归属、CP、歧义主体。"""
    return anchor_ctx(keywords, judge_cfg)


def _chart_hits(c: Candidate, songs: list[dict]) -> list[EntityHit]:
    """抖音文案里出现了榜单歌曲名 → 这条内容在用它当 BGM。

    短中文歌名（≤4 字）极易撞普通词（「慢倍速」就是视频里的描述语），
    所以要求它以 #话题 形式出现，或同现歌手名，否则不认。
    """
    text = f"{c.term} {' '.join(c.tags)}"
    low = text.lower()
    out: list[EntityHit] = []
    for song in songs:
        name = str(song.get("name") or "")
        if not song_in_text(text, song):
            continue
        out.append(
            EntityHit(name=name, etype="BGM", confidence=0.92)
        )
    return out[:3]


def _merge_same_material(materials: list[Material]) -> list[Material]:
    """同一素材的不同写法（撇号 / 全半角 / 空格）合并成一张卡。

    实测：`You're Under Arrest` 与 `You‘re Under Arrest` 曾各占一张卡，
    同一首歌既进热点又进候选。
    """
    out: dict[tuple[str, str], Material] = {}
    for mat in materials:
        key = (_norm(mat.name), mat.mtype)
        base = out.get(key)
        if base is None:
            out[key] = mat
            continue
        base.mentions += mat.mentions
        base.anchor_mentions += mat.anchor_mentions
        for a in mat.anchors:
            if a not in base.anchors:
                base.anchors.append(a)
        for p in mat.platforms:
            if p not in base.platforms:
                base.platforms.append(p)
        for link in mat.links:
            if link and link not in base.links and len(base.links) < 3:
                base.links.append(link)
        for s in mat.samples:
            if s and s not in base.samples and len(base.samples) < 3:
                base.samples.append(s)
        base.from_chart = base.from_chart or mat.from_chart
        if not base.chart:
            base.chart = mat.chart
            base.chart_rank = mat.chart_rank
        elif mat.chart_rank and (not base.chart_rank or mat.chart_rank < base.chart_rank):
            base.chart, base.chart_rank = mat.chart, mat.chart_rank
        if not base.artist or len(mat.artist) > len(base.artist):
            base.artist = mat.artist or base.artist
        if mat.circle == "圈内":
            base.circle = "圈内"
    return list(out.values())


def cmd_run(prod: bool, origin: str = "test") -> int:
    started = time.time()
    cfg = load_sources()
    keywords = load_keywords()

    run_cfg = cfg.get("run", {})
    judge_cfg = cfg.get("judge", {})
    tz = run_cfg.get("timezone", "Asia/Shanghai")
    now = now_local(tz)

    run_dir = new_run_dir(SANDBOX_DIR / "runs", now)
    # 运行标记：official = daily_run 触发的正式跑，test = 手动试跑
    write_json(
        run_dir / "run.json",
        {
            "kind": origin,
            "trigger": os.environ.get("RADAR_TRIGGER", ""),
            "date": now.strftime("%Y-%m-%d"),
            "created_at": now.isoformat(timespec="seconds"),
            "sent": False,
        },
    )
    _setup_logging(run_dir / "run.log")
    log.info("运行目录：%s", run_dir)

    # 模型层：三层各自有开关，任何一层失败都只跳过该层，不影响发信。
    llm_cfg = cfg.get("llm", {}) or {}
    llm_layers = llm_cfg.get("layers", {}) or {}
    llm = llm_from_config(llm_cfg, run_dir=run_dir)
    if llm_cfg.get("enabled"):
        if llm.available:
            log.info(
                "模型层：%s｜文档注入字符数 %s",
                llm.model, llm_context.describe(),
            )
        else:
            log.warning("模型层：已启用但不可用 —— %s；本轮全部走规则", llm.why_unavailable())

    fetcher = Fetcher(
        user_agent=run_cfg.get("user_agent", "Mozilla/5.0"),
        timeout=float(run_cfg.get("timeout_seconds", 20)),
        follow_redirects=bool(run_cfg.get("follow_redirects", True)),
    )
    try:
        candidates, statuses = _collect(fetcher, cfg, keywords, now.isoformat(timespec="seconds"))
    finally:
        fetcher.close()

    merged = dedupe(candidates)
    log.info("合并去重：%d → %d 条", len(candidates), len(merged))

    # ---- BGM 主源：音乐平台榜单（抖音网页版不暴露 music 字段，只能从这里拿「谁发了新歌」）
    chart_songs: list[dict] = []
    qq_cfg = cfg.get("sources", {}).get("qq_music", {})
    if qq_cfg.get("enabled"):
        t0 = time.time()
        try:
            res = qq_music.fetch(
                [str(c) for c in (qq_cfg.get("charts") or ["27"])],
                max_rank=int(qq_cfg.get("max_rank", 30)),
                qps_ms=int(qq_cfg.get("qps_ms", 800)),
            )
            chart_songs = res["songs"]
            statuses.append(
                SourceStatus(
                    id="qq_music", platform="qqmusic",
                    status="ok" if chart_songs else "failed",
                    count=len(chart_songs),
                    fields=["rank", "artists"],
                    elapsed_ms=int((time.time() - t0) * 1000),
                    note=("、".join(res["charts"]) if res["charts"] else "解析失败")
                    + (f"；{'/'.join(res['errors'])}" if res["errors"] else ""),
                )
            )
            log.info("音乐榜单：%d 首（%s）", len(chart_songs), "、".join(res["charts"]))
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(
                    id="qq_music", platform="qqmusic", status="failed",
                    elapsed_ms=int((time.time() - t0) * 1000), note=str(exc)[:80],
                )
            )
            log.warning("音乐榜单读取失败：%s", exc)

    # ---- 圈层标注：内容里有没有出现你关注的主体，决定素材走「圈内」还是「跨圈」
    subj_list = keyword_layer(keywords, "锚点层", "主体")
    amap = alias_map(keywords)
    subj_ctx = _subject_context(keywords, judge_cfg)
    circle_hits = 0
    for c in merged:
        if c.anchors:
            circle_hits += 1
            continue
        hit = _match_subject(
            f"{c.term} {' '.join(c.tags)}", subj_list, amap, **subj_ctx
        )
        if hit:
            c.anchors = [hit]
            circle_hits += 1
    log.info("圈内内容：%d / %d 条", circle_hits, len(merged))

    # 模板骨架抽取：先抽、再登记生命周期，判定时按词龄加权
    pcfg = pattern_config(keywords)
    matcher = PatternMatcher(
        pcfg["skeletons"], pcfg["context_words"], pcfg["self_validating"]
    )
    lifecycle = Lifecycle(DATA_DIR / "state" / "term_lifecycle.json")
    known = pcfg.get("known") or {}
    for word, month in known.items():
        lifecycle.seed(str(word), "", f"{month}-01")
    pruned = lifecycle.prune(set(matcher.families), {str(w) for w in known})
    if pruned:
        log.info("清理孤儿模板词：%d 条", pruned)
    today = now.strftime("%Y-%m-%d")
    hit_count = 0
    for c in merged:
        # 标签带上 # 再拼：骨架抽取优先从话题标签取完整词，边界才不会被截断
        text = f"{c.term} " + " ".join("#" + t for t in c.tags)
        hits = matcher.accept(
            text,
            matcher.extract(text),
            compact_source=(c.source == "douyin_hot"),
        )
        c.template_hits = [f"{h.word}@{h.family}" for h in hits]
        hit_count += len(hits)
        for h in hits:
            lifecycle.observe(h.word, h.family, today)
    if hit_count:
        log.info("模板骨架命中：%d 个实例", hit_count)

    # 实体抽取：把「内容」翻译成「素材」
    pairs = []
    entity_total = 0
    for c in merged:
        hits = entity.extract(c)
        if chart_songs:
            hits = hits + _chart_hits(c, chart_songs)
        # 抽出高置信度的模板/挑战名也登记生命周期
        for h in hits:
            if h.etype in {"模板", "挑战"} and h.confidence >= 0.6:
                lifecycle.observe(h.name, h.etype, today)
        entity_total += len(hits)
        pairs.append((c, hits))
    log.info("素材实体：%d 个（来自 %d 条内容）", entity_total, len(pairs))

    # ---- L2 模型素材理解：从一批文案里读出规则抽不出的具名素材
    # 它只是「多了一个抽取器」：产出的 EntityHit 照样走 aggregate → judge，
    # 门槛还是 mentions ≥2/3。evidence 里的 url 必须真的在输入中，否则整条作废。
    llm_material_stats: dict[str, Any] = {}
    mat_layer = llm_layers.get("material") or {}
    if llm.available and mat_layer.get("enabled"):
        blocked_names = set(keyword_layer(keywords, "锚点层", "主体"))
        for _alist in alias_map(keywords).values():
            blocked_names |= {a for a in _alist if a}
        batch = llm_material.extract_batch(
            llm,
            merged,
            blocked=blocked_names,
            max_items=int(mat_layer.get("max_items_per_query", 40)),
            min_items=int(mat_layer.get("min_items", 3)),
            max_calls=int(mat_layer.get("max_calls", 30)),
        )
        added = llm_material.apply_to_pairs(pairs, batch)
        # 模型抽出的词进词龄库的门槛比规则高（≥2 条独立证据 + 置信度 ≥0.7），
        # 否则模型每轮编一个新词就能污染词龄和跨天复用统计。
        registered = llm_material.register_lifecycle(lifecycle, batch.detail, today)
        llm_material_stats = {**batch.stats, "added_hits": added, "registered": registered}
        write_json(
            run_dir / "llm_material.json",
            {"stats": batch.stats, "added_hits": added, "detail": batch.detail},
        )
        log.info("L2 挂回：新增素材命中 %d 个", added)

    # 素材名撞主体名的（英文团名被当成曲名）直接剔除

    subj_norms = {_norm(x) for x in keyword_layer(keywords, "锚点层", "主体")}
    for aliases in alias_map(keywords).values():
        subj_norms |= {_norm(a) for a in aliases}

    clusters = cluster_captions(
        merged,
        only_sources={"douyin_search"},
        song_names={s["name"] for s in chart_songs},
        material_names={h.name for _, hits in pairs for h in hits},
        max_age_days=float(judge_cfg.get("recent_days", 7)),
    )
    if clusters:
        log.info("文案簇：%d 个", len(clusters))

    materials = aggregate(pairs, today=today, lifecycle=lifecycle)
    materials = [m for m in materials if _norm(m.name) not in subj_norms]

    # 把榜单歌曲的信息补到已有素材卡上；未被抖音用到的圈内新歌单独建卡
    chart_by_norm = {_norm(s["name"]): s for s in chart_songs}
    existing = {_norm(m.name) for m in materials}
    for mat in materials:
        song = chart_by_norm.get(_norm(mat.name))
        if not song:
            continue
        mat.from_chart = True
        mat.chart = str(song.get("chart") or "")
        mat.chart_rank = int(song.get("rank") or 0)
        artist = "、".join(song.get("artists") or [])
        if artist:
            mat.artist = artist
        if _match_subject(artist, subj_list, amap, **subj_ctx):
            mat.circle = "圈内"

    for song in chart_songs:
        key = _norm(song["name"])
        if key in existing:
            continue
        artist = "、".join(song.get("artists") or [])
        if not _match_subject(artist, subj_list, amap, **subj_ctx):
            continue          # 只有你关注的艺人，才给「零使用也输出」的待遇
        materials.append(
            Material(
                name=song["name"], mtype="BGM", artist=artist,
                usage_hint=["卡点", "挑战拍摄", "配乐"],
                mentions=0, from_chart=True,
                chart=str(song.get("chart") or ""),
                chart_rank=int(song.get("rank") or 0),
                circle="圈内",
                links=[song.get("url")] if song.get("url") else [],
            )
        )

    # 同一首歌可能因写法差异（撇号 / 全半角）各建一张卡，判定前按归一名合并
    materials = _merge_same_material(materials)
    # 同一玩法的不同写法（霸王龙舞步挑战 / 霸王龙舞步转场挑战）也合并，否则证据被拆散
    materials = merge_variants(materials)

    # 跨天复用：只有「连续第 3 天且热度没涨」的卡才让位，其余按额度限制
    recent_hot = load_recent_hot(
        DATA_DIR / "daily",
        today,
        days=int(judge_cfg.get("repeat_window_days", 4)),
    )
    if recent_hot:
        log.info("跨天复用：历史热点 %d 个可比对", len(recent_hot))

    # ---- L3 噪声误伤检测：找出「证据很多、却被判成噪声」的素材
    # 判据是矛盾而不是边界：一堆人在用的素材，不可能是无关领域。
    # 今天正式数据上通常是 0 个（词表刚修好），它只在词表失灵那天报警。
    llm_verdict_map: dict[str, str] = {}
    noise_alerts: list[dict[str, Any]] = []
    adj_layer = llm_layers.get("adjudicate") or {}
    if llm.available and adj_layer.get("enabled"):
        verdicts = llm_adjudicate.review(
            llm,
            materials,
            extra_noise=top_list(keywords, "噪声黑名单"),
            min_evidence=int(adj_layer.get("min_evidence", 3)),
            limit=int(adj_layer.get("max_calls", 25)),
        )
        for _keys, _v in ((verdicts.keep, "keep"), (verdicts.drop, "drop")):
            for _k in _keys:
                llm_verdict_map[_k] = _v
        noise_alerts = verdicts.alerts
        write_json(
            run_dir / "llm_verdicts.json",
            {"stats": verdicts.stats, "detail": verdicts.detail},
        )

    materials = judge_materials(
        materials,
        lifecycle=lifecycle,
        today=today,
        recent_days=int(judge_cfg.get("recent_days", 7)),
        max_output=int(judge_cfg.get("max_hotspots", 8)),
        per_anchor_cap=int(judge_cfg.get("per_anchor_cap", 2)),
        max_cross=int(judge_cfg.get("max_cross", 3)),
        extra_noise=top_list(keywords, "噪声黑名单"),
        recent_hot=recent_hot,
        max_repeat=int(judge_cfg.get("max_repeat_cards", 3)),
        repeat_growth=float(judge_cfg.get("repeat_growth_min", 0.2)),
        llm_verdicts=llm_verdict_map or None,
    )
    lifecycle.save()

    hot = sorted([m for m in materials if m.cls == "热点"], key=lambda m: m.order)
    log.info(
        "素材卡：输出 %d 张 / 候选 %d / 丢弃 %d",
        len(hot),
        sum(1 for m in materials if m.cls == "候选"),
        sum(1 for m in materials if m.cls == "丢弃"),
    )

    # 丢弃留痕：日报只显示前若干条，复盘要看全量。
    # 「证据 ≥3 条却被丢」是噪声词表失灵的最强信号 —— 2026-11-09 早上的
    # SUPERSTAR（16 条在用被判体育领域）就是这一类，当时只能靠人翻日志发现。
    dropped_all = sorted([m for m in materials if m.cls == "丢弃"], key=lambda m: -m.mentions)
    dropped_high = [m for m in dropped_all if m.mentions >= 3]
    write_json(
        run_dir / "dropped.json",
        {
            "total": len(dropped_all),
            "high_evidence": len(dropped_high),
            "items": [
                {
                    "name": m.name, "type": m.mtype, "mentions": m.mentions,
                    "circle": m.circle, "reason": m.reason,
                    "llm_verdict": m.llm_verdict, "samples": m.samples[:2],
                }
                for m in dropped_all
            ],
        },
    )
    if dropped_high:
        log.warning(
            "有 %d 张素材证据 ≥3 条却被判丢弃，建议确认词表是否误伤：%s",
            len(dropped_high),
            "、".join(f"{m.name}({m.mentions}条/{m.reason[:12]})" for m in dropped_high[:5]),
        )

    # BGM 采样：给「素材」补配乐，不逐条抓（详见 collectors/bgm_sampler.py）
    bgm_cfg = cfg.get("sources", {}).get("bgm_sampler", {})
    if bgm_cfg.get("enabled"):
        # 素材卡可能由多条实体合并而来（同义写法 / 近义变体），合并后的名字
        # 不一定在 hits 里出现过。按「当前名 + 归一名 + 别名」建反查表，
        # 否则合并卡会被当成「无非抖音来源」而跳过采样。
        name_to_mat: dict[str, Material] = {}
        for mat in materials:
            if mat.cls != "热点":
                continue
            for alias in (mat.name, _norm(mat.name), *mat.variants):
                if alias:
                    name_to_mat.setdefault(alias, mat)

        cands_by_name: dict[str, list[Candidate]] = {}
        seen_pairs: set[tuple[str, int]] = set()
        for cand, hits in pairs:
            for h in hits:
                mat = name_to_mat.get(h.name) or name_to_mat.get(_norm(h.name))
                if mat is None:
                    continue
                key = (mat.name, id(cand))
                if key in seen_pairs:      # 同一张卡的两个别名命中同一条视频时只算一次
                    continue
                seen_pairs.add(key)
                cands_by_name.setdefault(mat.name, []).append(cand)

        cache_path = ROOT / str(bgm_cfg.get("cache", "data/state/bgm_cache.json"))
        bgm_cache = bgm_sampler.load_cache(
            cache_path, ttl_days=float(bgm_cfg.get("cache_ttl_days", 7))
        )
        bgm_plan = bgm_sampler.plan(
            materials,
            cands_by_name,
            chart_songs=chart_songs,
            cache=bgm_cache,
            daily_budget=int(bgm_cfg.get("daily_budget", 60)),
            per_material=int(bgm_cfg.get("per_material", 1)),
            infer_ratio=float(bgm_cfg.get("infer_ratio", 0.6)),
        )
        bgm_stats = bgm_sampler.enrich(
            materials,
            bgm_plan.tasks,
            base_url=str(bgm_cfg.get("base_url", "https://api.sandbase.ai")),
            endpoint=str(bgm_cfg.get("endpoint", "/v1/api/douyin/app-v3/one-video-v3")),
            timeout=float(bgm_cfg.get("timeout_seconds", 15)),
            cache=bgm_cache,
        )
        bgm_sampler.save_cache(cache_path, bgm_cache)
        write_json(
            run_dir / "bgm_plan.json",
            {"tasks": bgm_plan.tasks, "skipped": bgm_plan.skipped, "stats": bgm_stats},
        )
        log.info(
            "BGM 采样：计划 %d｜推断 %d｜调用 %d（成功 %d，占位/原声 %d）｜跳过 %s",
            len(bgm_plan.tasks), bgm_plan.inferred,
            bgm_stats["calls"], bgm_stats["ok"], bgm_stats.get("placeholder", 0),
            bgm_plan.skipped,
        )

    # 复用短语：文案梗的发现线索（整句聚类抓不到「半句相同」的变体）
    # 两道过滤：B 规则层（作品语境）→ D 标签层（模型判定过的 title/noise 直接剔除）
    labels_path = DATA_DIR / "state" / "phrase_labels.json"
    phrase_labels: dict[str, str] = {}
    if labels_path.exists():
        try:
            phrase_labels = {
                str(k): str(v)
                for k, v in json.loads(labels_path.read_text(encoding="utf-8")).items()
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("短语标签读取失败：%s", exc)

    phrase_args = dict(
        exclude_names={m.name for m in materials} | {s["name"] for s in chart_songs},
        exclude_phrases=set(keyword_layer(keywords, "形式层", "创作类型词"))
        | set(keyword_layer(keywords, "形式层", "场景词")),
        min_videos=int(judge_cfg.get("phrase_min_videos", 3)),
        # 圈层信号：短语出现在多少条「提到关注主体」的内容里。
        # 主体名 + 别名一起算，否则「A2O」这种缩写会漏判。
        subject_names={
            n for n in (
                set(keyword_layer(keywords, "锚点层", "主体"))
                | set(alias_map(keywords))
                | {a for vs in alias_map(keywords).values() for a in vs}
            )
            if n and len(n) >= 2
        },
    )
    phrases = top_phrases(
        [c for c in merged if c.source == "douyin_search"],
        labels=phrase_labels,
        **phrase_args,
    )

    # 短语的跨天追踪：先读「首见于几天前」，再登记今天。
    # 顺序不能反 —— observe 会把新词的 first_seen 设成今天，先登记就读不到年龄了。
    # 为什么需要它：内容级门槛把 age_days 压在 0~7 天，光看内容年龄分不出老梗；
    # 只有「这个短语在我们数据里存在了多久」才能回答「是不是几个月前的老梗」。
    for p in phrases:
        p["first_seen_days"] = lifecycle.age_days(str(p["phrase"]), today)
    for p in phrases:
        lifecycle.observe(str(p["phrase"]), "phrase", today)
    if phrases:
        # 上面的 lifecycle.save() 早于这里执行，不补一次的话短语登记会丢
        lifecycle.save()

    # 文案可用性评估：这批句子能不能当二创文案用、怎么用。
    # 与上面的「待判定」是两件事：那个是数据清洁（长期），这个是产品判断（当天）。
    phrase_eval: list[dict] = []
    ph_layer = llm_layers.get("phrase") or {}
    if llm.available and ph_layer.get("enabled") and phrases:
        phrase_eval, eval_stats = llm_phrases.evaluate(
            llm,
            phrases,
            material_names=[m.name for m in hot] + [s["name"] for s in chart_songs],
            limit=int(ph_layer.get("max_items", 12)),
        )
        write_json(
            run_dir / "phrase_eval.json",
            {"stats": eval_stats, "items": phrase_eval},
        )

    # 待模型判定的短语：跳过 B 规则，看看规则挡掉了什么、又漏了什么
    todo = [
        p for p in top_phrases(
            [c for c in merged if c.source == "douyin_search"],
            labels=phrase_labels,
            ignore_works=True,
            **phrase_args,
        )
        if p["phrase"] not in phrase_labels
    ][:10]
    if todo:
        write_json(run_dir / "phrases_todo.json", todo)
        log.info("复用短语：%d 个（待判定 %d 个）", len(phrases), len(todo))
    elif phrases:
        log.info("复用短语：%d 个", len(phrases))

    # 今日新歌：你关注主体的榜单新歌，单列一栏（不占素材卡名额）
    # 只收新歌榜 / 飙升榜：热歌榜是存量热度，老歌不该出现在「今日新歌」里
    chart_news = []
    for song in sorted(chart_songs, key=lambda s: s.get("rank", 999)):
        if not any(k in str(song.get("chart") or "") for k in NEW_CHARTS):
            continue
        artist = "、".join(song.get("artists") or [])
        if not _match_subject(artist, subj_list, amap, **subj_ctx):
            continue
        chart_news.append(
            {
                "name": song["name"],
                "artist": artist,
                "rank": int(song.get("rank") or 0),
                "chart": song.get("chart", ""),
            }
        )
    chart_news = chart_news[:8]

    # 模型层的状态也要进日报：跑没跑、调了几次、缓存命中多少，一眼能看出来。
    # 没配 key 时不报错，只在报告里写「不可用 + 原因」。
    if llm_cfg.get("enabled"):
        llm.save_cache()
        statuses.append(
            SourceStatus(
                id="llm",
                platform="llm",
                status="ok" if llm.available else "skipped",
                count=int(llm.stats.get("ok") or 0),
                fields=["json"],
                note=(
                    f"{llm.model}｜调用 {llm.stats['calls']}（缓存 {llm.stats['cached']}，"
                    f"失败 {llm.stats['fail']}）｜token 入 {llm.stats['in_tokens']}"
                    f" / 出 {llm.stats['out_tokens']}"
                    if llm.available
                    else llm.why_unavailable()
                ),
                detail={
                    "layers": {k: bool((v or {}).get("enabled")) for k, v in llm_layers.items()},
                    "material": llm_material_stats,
                    "verdicts": llm_verdict_map,
                    "noise_alerts": [
                        {"name": a["name"], "mentions": a["mentions"],
                         "noise_reason": a.get("noise_reason", "")}
                        for a in noise_alerts
                    ],
                },
            )
        )

    elapsed = time.time() - started
    report = render_report(
        date_str=now.strftime("%Y-%m-%d"),
        generated_at=now.strftime("%Y-%m-%d %H:%M:%S"),
        elapsed_s=elapsed,
        statuses=statuses,
        materials=materials,
        total_candidates=len(merged),
        entity_count=entity_total,
        clusters=clusters,
        phrases=phrases,
        phrase_eval=phrase_eval,
        chart_news=chart_news,
    )

    write_text(run_dir / "report.md", report)
    write_json(run_dir / "candidates.json", [c.model_dump() for c in merged])
    write_json(run_dir / "materials.json", [m.model_dump() for m in materials])
    # 邮件/网页渲染用的旁路数据：文案簇、复用短语、今日新歌原本只在内存里
    write_json(
        run_dir / "report_data.json",
        {
            # size 是 dataclass 的 property，asdict 不会带上，单独补
            "clusters": [{**asdict(c), "size": c.size} for c in clusters],
            "phrases": phrases,
            "phrase_eval": phrase_eval,
            "chart_news": chart_news,
        },
    )
    write_json(
        run_dir / "meta.json",
        {
            "date": now.strftime("%Y-%m-%d"),
            "generated_at": now.isoformat(timespec="seconds"),
            "elapsed_s": round(elapsed, 2),
            "candidates": len(merged),
            "entities": entity_total,
            "materials": len(materials),
            "sources": [s.model_dump() for s in statuses],
            "llm": (
                {
                    "model": llm.model,
                    "available": llm.available,
                    "stats": llm.stats,
                    "material": llm_material_stats,
                    "verdicts": llm_verdict_map,
                }
                if llm_cfg.get("enabled")
                else None
            ),
        },
    )

    if prod:
        # 写到本轮目录，由 daily_run 在「邮件发出后」复制到 data/daily/<date>/
        # 这样 data/daily 里只会出现真正发出去的日报
        out = run_dir / "daily.json"
        write_json(
            out,
            {
                "meta": {
                    "date": now.strftime("%Y-%m-%d"),
                    "generated_at": now.isoformat(timespec="seconds"),
                    "sources": [s.model_dump() for s in statuses],
                },
                "materials": [
                    {
                        "素材": m.name,
                        "类型": m.mtype,
                        "可用于": m.usage_hint,
                        "主体": m.artist,
                        "证据": m.reason,
                        "链接": m.links,
                    }
                    for m in hot
                ],
                "candidates": [
                    {"素材": m.name, "类型": m.mtype, "原因": m.reason}
                    for m in materials if m.cls == "候选"
                ],
                "stats": {
                    "content": len(merged),
                    "entities": entity_total,
                    "materials": len(hot),
                },
            },
        )
        log.info("正式产物：%s", out)

    # 分层清理：每天只留最新一轮的重文件，7 天前的全删，避免 sandbox 无限膨胀
    cleanup_cfg = cfg.get("cleanup", {}) or {}
    pstats = prune(
        SANDBOX_DIR / "runs",
        heavy_days=int(cleanup_cfg.get("heavy_days", 7)),
        log_days=int(cleanup_cfg.get("log_days", 30)),
        diag_days=int(cleanup_cfg.get("diag_days", 3)),
        diag_max=int(cleanup_cfg.get("diag_max", 50)),
        max_days=int(cleanup_cfg.get("max_days", 0)),
    )
    if pstats["freed_bytes"]:
        log.info(
            "清理：释放 %.1f MB（删除目录 %d / 日志 %d / 截图 %d）",
            pstats["freed_bytes"] / 1024 / 1024,
            pstats["dropped_dirs"], pstats["logs"], pstats["diag"],
        )

    print()
    print(f"✅ 完成：素材卡 {len(hot)} 张 / 内容 {len(merged)} 条 / 用时 {elapsed:.1f}s")
    print(f"📄 日报：{run_dir / 'report.md'}")
    return 0


def cmd_clean(days: int) -> int:
    removed = clean_old(SANDBOX_DIR / "runs", days)
    print(f"已清理 {len(removed)} 个运行目录（早于 {days} 天）")
    for name in removed:
        print(f"  - {name}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    cfg = (load_sources().get("cleanup", {}) or {})
    stats = prune(
        SANDBOX_DIR / "runs",
        heavy_days=args.heavy_days if args.heavy_days is not None else int(cfg.get("heavy_days", 7)),
        log_days=args.log_days if args.log_days is not None else int(cfg.get("log_days", 30)),
        diag_days=args.diag_days if args.diag_days is not None else int(cfg.get("diag_days", 3)),
        diag_max=int(cfg.get("diag_max", 50)),
        max_days=int(cfg.get("max_days", 0)),
    )
    print(f"清理完成：释放 {stats['freed_bytes'] / 1024 / 1024:.1f} MB"
          f"（目录 {stats['dropped_dirs']} / 日志 {stats['logs']} / 截图 {stats['diag']}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    parser = argparse.ArgumentParser(prog="radar", description="追星二创素材雷达")
    parser.add_argument("--run", action="store_true", help="跑一次抓取 + 判定 + 生成日报")
    parser.add_argument("--prod", action="store_true", help="额外产出正式 JSON（daily.json）")
    parser.add_argument("--origin", default="test", choices=["test", "official"],
                        help="运行来源：official=daily_run 正式跑，test=手动试跑")
    parser.add_argument("--clean", action="store_true", help="清理旧运行目录（整体删除）")
    parser.add_argument("--prune", action="store_true", help="分层清理：只删重文件，保留日报")
    parser.add_argument("--heavy-days", type=int, default=None, help="重文件保留天数（默认 7）")
    parser.add_argument("--log-days", type=int, default=None, help="日志保留天数（默认 30）")
    parser.add_argument("--diag-days", type=int, default=None, help="失败截图保留天数（默认 3）")
    parser.add_argument("--days", type=int, default=7, help="--clean 时的保留天数")
    args = parser.parse_args(argv)

    if args.prune:
        return cmd_prune(args)
    if args.clean:
        return cmd_clean(args.days)
    if args.run:
        return cmd_run(args.prod, args.origin)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

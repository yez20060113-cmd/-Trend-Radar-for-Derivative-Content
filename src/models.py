"""候选对象的数据模型。所有采集器都必须产出 Candidate，字段缺失要显式记录。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    """一条待判定内容（词条 / 视频 / 标签集合）。"""

    # 原始信息
    term: str
    author: str = ""                   # 作者/账号，用于「≥2 个不同作者」门槛
    platform: str                      # douyin / bilibili
    source: str                        # douyin_hot / bili_search ...
    url: str | None = None
    rank: int | None = None
    tags: list[str] = Field(default_factory=list)

    # 数值字段：缺失就不写入（缺字段 ≠ 值为 0）
    field: dict[str, Any] = Field(default_factory=dict)

    # 采集上下文
    anchors: list[str] = Field(default_factory=list)   # 组合搜索使用的主体（已通过命中校验）
    combo: str | None = None
    fetched_at: str | None = None

    # 合并后填充
    term_norm: str = ""
    platforms: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    template_hits: list[str] = Field(default_factory=list)   # "词@骨架"，由骨架抽取填充

    def finalize(self) -> "Candidate":
        """补全合并字段的默认值。"""
        if not self.platforms:
            self.platforms = [self.platform]
        if not self.sources:
            self.sources = [self.source]
        return self


class SourceStatus(BaseModel):
    """单个数据源的运行状态。缺字段 ≠ 没证据，必须显式区分。"""

    id: str
    platform: str
    status: str = "ok"                 # ok / failed / skipped
    count: int = 0
    fields: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0                # 只写进 meta.json，供事后排障看哪个源慢
    note: str = ""
    # 结构化补充，供投递层判断「是否只覆盖了一部分数据」
    # 例：{"queries_total": 40, "queries_ok": 15, "queries_failed": 25}
    detail: dict[str, Any] = Field(default_factory=dict)


class EntityHit(BaseModel):
    """从一条内容里抽出的素材实体（歌 / 模板 / 挑战）。"""

    name: str
    etype: str                        # BGM / 模板 / 挑战
    confidence: float = 0.5
    origin: str = ""                  # "" = 规则抽取；"llm" = 模型理解层产出


class Material(BaseModel):
    """素材卡：判定的真正单位。多条内容提到同一个素材 → 合并成一张卡。"""

    name: str
    mtype: str                        # BGM / 模板 / 挑战
    artist: str = ""                  # 歌才有
    usage_hint: list[str] = Field(default_factory=list)
    mentions: int = 0                 # 被多少条内容提到
    anchor_mentions: int = 0          # 其中多少条真的命中了主体（圈层证据的分母/分子）
    platforms: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    signals: dict[str, float] = Field(default_factory=dict)
    from_board: bool = False           # 是否来自榜单条目（榜单本身即"当下在火"的旁证）
    from_chart: bool = False           # 是否来自音乐平台榜单（新歌/热歌）
    chart: str = ""                    # 榜单名（新歌榜 / 飙升榜 / 热歌榜）
    chart_rank: int = 0                # 音乐榜单排名
    circle: str = ""                   # 圈内 / 跨圈：决定门槛高低
    bgm_title: str = ""                # 这个素材的配乐（歌名）
    bgm_author: str = ""               # 配乐歌手
    bgm_source: str = ""               # inferred / api
    links: list[str] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)   # 合并进来的其他写法
    repeat_days: int = 0              # 连续第几天出现（1 = 首次；0 = 无历史数据）
    repeat: bool = False              # 是否属于「昨日已有」的复用卡（受单日额度限制）
    llm_verdict: str = ""             # 模型复核：keep / drop（空 = 未送审）
    cls: str = ""                     # 热点 / 候选 / 丢弃
    reason: str = ""
    score: float = 0.0
    order: int = 0

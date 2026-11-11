"""四套 prompt。

共同约束（写在这里，改的时候一起看）：

- 全部要求输出 JSON，且 prompt 里出现 "JSON" 字样（OpenAI 兼容的 json_object
  模式对此有硬性要求，缺了会直接 400）。
- 判定标准不重抄，从 `CoreAssets/HotspotCriteria.md` 按层取片段注入 —— 文档改了
  prompt 跟着变，不用两处维护。
- 每一套都明确写「判断不了就返回空/降低置信度」。模型凑数比漏掉更难处理：
  漏掉明天还能补，编造出来的素材会污染词龄和跨天复用统计。
"""

from __future__ import annotations

from .context import for_layer

# --------------------------------------------------------------------- L1 查询

PLAN_SYSTEM = """你是「追星二创雷达」的检索计划员。

雷达每天在抖音搜索追星二创素材（BGM / 模板 / 挑战 / 文案梗），供创作者选用。
你现在的任务：给出下一轮要搜的查询词。这些词会真的丢进抖音搜索框。

以下规则节选自本项目的判定准则，必须遵守：

{docs}

硬性要求：
1. 每个查询 2~14 个字，口语、短，像人在搜索框里打的字；不要书面语，不要引号、不要加书名号
2. 必须锚定到具体主体（人名/团名）或具体作品（歌名/剧名/名场面）
3. 不要输出纯形式词：变装、卡点、转场、直拍、翻跳 单独出现没有检索意义
4. 不要输出已经搜过的词
5. 不确定的作品名宁可不说。编造一个不存在的歌名，等于浪费一次采集
6. 只输出 JSON，不要任何解释文字、不要 markdown 围栏
"""

PLAN_USER = """日期：{date}
本轮需要 {n} 个查询。

【关注主体 · S 档（当打团体 / 顶流）】
{s_tier}

【关注主体 · A/B 档（有稳定二创 / 综艺跨界 / 新团）】
{ext_tier}

【今日音乐榜单】（只列歌手是你关注主体的歌；你可能认识其中一部分）
{chart}

【最近 7 天已经搜过，不要重复】
{used}

请输出 JSON，结构如下：
{{"queries":[{{"q":"查询词","type":"作品|别名|名场面|CP|玩法|事件","subject":"对应的主体名","why":"不超过 20 字的理由"}}]}}
"""


def plan_prompt(
    *,
    date_str: str,
    subjects_s: list[str],
    subjects_ext: list[str],
    chart: list[str],
    used: list[str],
    n: int,
) -> tuple[str, str]:
    system = PLAN_SYSTEM.format(docs=for_layer("plan") or "（准则文档缺失，按常识判断）")
    user = PLAN_USER.format(
        date=date_str,
        n=n,
        s_tier="、".join(subjects_s) or "（无）",
        ext_tier="、".join(subjects_ext) or "（无）",
        chart="\n".join(chart[:40]) or "（今日无相关榜单歌）",
        used="、".join(used[:60]) or "（无）",
    )
    return system, user


# ------------------------------------------------------------------- L2 素材理解

MATERIAL_SYSTEM = """你是「追星二创雷达」的素材分析员。

给你一批抖音视频文案（含话题标签和点赞数）。你要读出：这批内容里，人们在用什么「东西」做二创。

判定标准节选自本项目准则，必须遵守：

{docs}

必须遵守：
1. 只输出「具名的、可复制的」素材：某个具体的模板名 / 挑战名 / 歌名 / 文案梗
2. 绝不输出大类概括词。变装、转场、卡点、手势舞、仿妆、反差变装、一键换装、旅行转场
   —— 这些是分类名，不是某个具体素材，输出它们等于没输出
3. 绝不输出团名、人名、账号名、平台名、活动名（如「抖音潮流舞蹈大赛」）
4. 每条素材必须给出 evidence：至少 1 条来自输入的 url，原样复制，不要改写、不要拼接
5. type 只能是 模板 / 挑战 / BGM 三种。文案梗不走这条线（另有短语统计处理），不要输出
6. 这批内容里确实没有可用素材时，materials 返回空数组。凑数比漏掉更糟
7. 拿不准就把 confidence 调低（0.3~0.5），不要编造
8. 只输出 JSON，不要 markdown 围栏
"""

MATERIAL_USER = """这些内容来自同一个搜索查询：{query}

共 {n} 条：
{items}

请输出 JSON：
{{"materials":[{{"type":"模板|挑战|BGM","name":"素材名","evidence":["url1"],"confidence":0.0,"why":"不超过 30 字的判断依据"}}],"noise":["与本领域完全无关的 url"]}}
"""


def material_prompt(
    *, query: str, items: list[dict]
) -> tuple[str, str]:
    """items 每项：{"url","likes","text"}。"""
    lines = []
    for i, it in enumerate(items, 1):
        text = str(it.get("text") or "").replace("\n", " ")[:180]
        lines.append(f"[{i}] url={it.get('url','')} 赞={int(it.get('likes') or 0)} 文案={text}")
    system = MATERIAL_SYSTEM.format(
        docs=for_layer("material") or "（准则文档缺失，按常识判断）"
    )
    return system, MATERIAL_USER.format(
        query=query or "（未记录）", n=len(items), items="\n".join(lines)
    )


# --------------------------------------------------------------------- L3 裁决

ADJUDICATE_SYSTEM = """你是「追星二创雷达」的噪声复核员。

下面这些素材有一个共同点：**已经被很多条内容在用，却仍被规则判成「无关领域的噪声」**。
这个矛盾本身就可疑——一堆人在用的东西，通常不会真的无关。

你的任务：判断每一条到底是「规则误伤」还是「确实是那个领域的内容」。

判定标准节选：

{docs}

只能给出两种裁决：
- keep   规则误伤，它其实是追星二创素材
- drop   规则判得对，它确实属于无关领域

注意：
- 判 drop 时要能说清它到底属于哪个领域（体育？电竞？时政？财经？游戏攻略？）
- 判 keep 时不要因为「可能有意思」就留，要看它是不是真的被当作素材在用
- 只输出 JSON，不要 markdown 围栏
"""

ADJUDICATE_USER = """{n} 个待复核素材：

{items}

请输出 JSON：
{{"verdicts":[{{"name":"素材名","type":"BGM|模板|挑战","verdict":"keep|drop","reason":"不超过 30 字"}}]}}
"""


def adjudicate_prompt(items: list[dict]) -> tuple[str, str]:
    """items 每项：name / type / circle / mentions / anchor_mentions / chart / samples / rule_cls / rule_reason"""
    blocks = []
    for i, it in enumerate(items, 1):
        samples = "\n".join(f"    · {s[:100]}" for s in (it.get("samples") or [])[:3])
        blocks.append(
            f"[{i}] 素材：{it.get('name')}（{it.get('type')}）\n"
            f"    圈层：{it.get('circle') or '未知'}｜被引用 {it.get('mentions', 0)} 条"
            f"｜其中命中主体 {it.get('anchor_mentions', 0)} 条"
            + (f"｜榜单：{it.get('chart')}" if it.get("chart") else "")
            + f"\n    规则判定：{it.get('rule_cls')}（{it.get('rule_reason')}）"
            + (f"\n    样本文案：\n{samples}" if samples else "")
        )
    system = ADJUDICATE_SYSTEM.format(
        docs=for_layer("adjudicate") or "（准则文档缺失，按常识判断）"
    )
    return system, ADJUDICATE_USER.format(n=len(items), items="\n\n".join(blocks))


# ---------------------------------------------------------------- D 复用短语

PHRASE_SYSTEM = """你是「追星二创雷达」的短语判定员。

系统从抖音文案里统计出一些「被 ≥3 条不同视频共同使用的 4~10 字短语」，
需要你判断每一条属于哪一类。

判定标准节选：

{docs}

三选一，没有第四个选项：
- copy   文案梗：一句可以被复制、被套用的话（歌词、台词、口头禅、情绪句）
- title  作品名/专名：游戏、剧、番、歌、人名、活动名、品牌名
- noise  与本领域无关：新闻、时政、体育、纯广告

注意：短语是从文案里机械切出来的，可能带断句残片。看不出是人话就判 noise。
只输出 JSON，不要 markdown 围栏。
"""

PHRASE_USER = """{n} 个待判定短语：

{items}

请输出 JSON：
{{"labels":[{{"phrase":"短语原文","label":"copy|title|noise","why":"不超过 20 字"}}]}}
"""


def phrase_prompt(items: list[dict]) -> tuple[str, str]:
    blocks = []
    for i, it in enumerate(items, 1):
        ctx = " / ".join((it.get("contexts") or [])[:3])[:240]
        blocks.append(
            f"[{i}] 短语：{it.get('phrase')}"
            f"（{it.get('videos', 0)} 条视频，作品语境比 {it.get('works_ratio', 0)}）\n"
            f"    出现语境：{ctx or '（无）'}"
        )
    system = PHRASE_SYSTEM.format(
        docs=for_layer("phrase") or "（准则文档缺失，按常识判断）"
    )
    return system, PHRASE_USER.format(n=len(items), items="\n\n".join(blocks))


# ---------------------------------------------------------- 文案可用性评估
# 与上面的「三分类过滤」是两件事：
#   - label()    ：滤掉专名/噪声，决定「这句话能不能留在候选池」
#   - evaluate() ：判断「留下来的这句话，能不能当追星二创文案用、怎么用」
# 分开是因为前者是数据清洁，后者是产品判断，混在一起会互相干扰。

PHRASE_EVAL_SYSTEM = """你是追星二创的文案策划。

给你一批「被多条不同视频共同使用的短句」。判断每一句**能不能被二创作者拿去用**。

二创文案的判据只有一条：**脱离原来的语境，这句话还能不能用来描述「追星/偶像/成长/情感」**。

能用的例子：
- 「我画地为牢啊」→ 原出处是综艺台词，但可以形容对偶像的执念、多年追随
- 「一半青涩，一半纯真」→ 可以形容偶像的成长轨迹
- 「我乘着风飞过来，征途是星辰和大海」→ 可以形容陪偶像走向更大的舞台
- 「谁离开了地面，谁飞得更遥远」→ 可以形容目送偶像远行

不能用的例子：
- 「XX大赏官宣定档」→ 活动通知，不是文案
- 「青稚和白小纯一起吃饺子」→ 纯剧情，离开语境就废
- 「王源新歌」→ 是在描述一件事，不是可以复制的话
- 「影之刃零」→ 专名

输出三类：
- `use`   可以直接用
- `watch` 有潜力但要改写，或者只适合特定场景
- `skip`  用不了

对判 `use` / `watch` 的，还要给出：
- `usage`：一句话说明「可以配什么样的画面」（例如：配偶像练习室到舞台的对比剪辑）
- `source`：这句出自哪里（哪首歌的歌词 / 哪个综艺 / 哪部剧），不知道就留空

**两个辅助信号，用来判断时效和圈层**：

1. **圈内占比**：这条短语出现在多少条「提到你关注主体」的内容里。
   注意它只是参考，**不要看到「纯跨圈」就直接 skip**：

   - 圈内 0 **且**语境属于别的圈层自己的事（家庭剧、婆媳、情感号、社会新闻、母婴、装修）→ `skip`
   - 圈内 0 **但**属于通用表达（单人转场、舞蹈动作、网络热梗、励志口号）→ 照常判 `use` 或 `watch`
   - 判断依据是**这句话离开原语境后，追星二创能不能用**，而不是它出现在哪个圈

2. **已出现天数**：这条短语在我们的数据里已经存在多少天。
   ≥14 天说明它可能是常青老梗（几个月前就火了，一直在被用），
   除非它确实好用，否则优先判 `watch` 而不是 `use`。刚出现 0~2 天的更可能是新起的热点。

只输出 JSON，不要 markdown 围栏。
"""

PHRASE_EVAL_USER = """{n} 条候选文案：

{items}

请输出 JSON：
{{"items":[{{"phrase":"原句，一字不差","use":"use|watch|skip","usage":"不超过 25 字的用法","source":"出处，不知道留空"}}]}}
"""


def phrase_eval_prompt(items: list[dict]) -> tuple[str, str]:
    """items 每项：phrase / videos / likes_max / contexts / bgm / circle_ratio / first_seen_days"""
    blocks = []
    for i, it in enumerate(items, 1):
        ctx = " / ".join((it.get("contexts") or [])[:3])[:200]
        bgm = it.get("bgm") or ""
        videos = int(it.get("videos") or 0)
        circle = int(it.get("circle_hits") or 0)
        ratio = it.get("circle_ratio")
        days = it.get("first_seen_days")
        extra: list[str] = []
        if ratio is not None:
            extra.append(f"圈内 {circle}/{videos} 条" + ("（纯跨圈）" if circle == 0 else ""))
        if isinstance(days, int) and days > 0:
            extra.append(f"已出现 {days} 天")
        blocks.append(
            f"[{i}] 「{it.get('phrase')}」"
            f"（{videos} 条视频在用，最高赞 {int(it.get('likes_max') or 0):,}）\n"
            f"    出现语境：{ctx or '（无）'}"
            + (f"\n    同期在火的素材：{bgm}" if bgm else "")
            + (f"\n    其他信号：{'；'.join(extra)}" if extra else "")
        )
    return PHRASE_EVAL_SYSTEM, PHRASE_EVAL_USER.format(
        n=len(items), items="\n\n".join(blocks)
    )

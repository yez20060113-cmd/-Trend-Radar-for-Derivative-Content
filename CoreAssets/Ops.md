---
title: Ops
summary: 运维手册——目录与文件职责、环境准备、日常跑批、故障排查
---

## 一、这个项目是什么

每天跑一次，从抖音/B站采集内容，抽出**可复用的素材**（歌 / 模板 / 挑战）和被**批量复制的文案**，生成一份 Markdown 日报。

产出两条线：

- **素材卡**：NEON（BGM）、霸王龙舞步转场挑战（挑战）
- **文案模板**：世界如此辽阔 请不要走进悲伤角落（被 6 条视频复制）

## 二、目录与文件职责

### 文档（`CoreAssets/`）

| 文件 | 职责 | 什么时候读 |
|---|---|---|
| `AgentRole.md` | 总控：角色、文件索引、读取规则、工作路径 | 每次开工先读 |
| `InputOutput.md` | 数据契约：输入格式、字段用途、词库用法、输出格式 | 采集前 / 输出前 |
| `HotspotCriteria.md` | 判定准则：抽取规则、数据门槛、两条输出线的判定 | 判定时 |
| `Ops.md` | 本文件：怎么装、怎么跑、坏了怎么查 | 部署 / 排障时 |
| `keywords.json` | 词库：锚点层（主体 / 主体画像 / 成员归属 / 厂牌 / CP / 歌名）、时效层、形式层、模式层、别名、黑名单 | 采集前加载 |
| `keywords.bak.json` | 扩充前的词库备份 | 回滚时 |

### 代码（`src/`）

| 路径 | 职责 |
|---|---|
| `main.py` | 命令行入口，串起采集 → 过滤 → 抽取 → 聚合 → 判定 → 渲染 → 落盘 |
| `config.py` | 读 `config/sources.yaml` 与 `keywords.json` |
| `models.py` | 数据模型：`Candidate`（一条内容）、`Material`（素材卡）、`EntityHit`、`SourceStatus` |
| `collectors/http.py` | 统一 HTTP：超时、重试、单源频控 |
| `collectors/douyin_hot.py` | 抖音热榜（**综合新闻榜**；偶尔出现挑战词，不是主要来源，实测常为 0） |
| `collectors/douyin_search.py` | 读 Agent 采集好的抖音搜索结果 JSON |
| `collectors/bgm_sampler.py` | BGM 采样：按素材抽代表视频、预算控制、缓存、推断 |
| `collectors/qq_music.py` | 抓 QQ音乐榜单（新歌/飙升/热歌三榜），解析 JSON-LD → BGM 主源 |
| `collectors/bili_search.py` | B站组合搜索 + **主体命中校验**（交叉验证用） |
| `normalize/term.py` | 词条归一化（去 # / emoji / 标点 / 空格） |
| `normalize/merge.py` | 跨源合并去重（同 term 合并，`platforms` 记录跨平台） |
| `normalize/pattern.py` | 模板骨架抽取（`{X}变装` / `{X}转场` …） |
| `normalize/entity.py` | 从标题抽素材实体：歌 / 模板 / 挑战 |
| `normalize/aggregate.py` | 跨内容聚合 + 近义变体合并（剥骨架后缀比本体）→ 素材卡 |
| `normalize/cluster.py` | 文案 3-gram 包含度聚类 → 文案簇（带宣发/素材/时效三道闸门） |
| `normalize/songmatch.py` | 歌名匹配共用规则（区分「真提到」与撞词） |
| `normalize/phrase.py` | 复用短语统计 → 文案梗发现线索（4~10 字中文短语） |
| `judge/threshold.py` | 内容级数据门槛（L0 点赞 / L1 分位） |
| `judge/works.py` | 作品语境判据：判断短语是「文案梗」还是「作品名」（游戏/影视/动漫） |
| `judge/domain.py` | 领域噪声词表（体育/电竞/时政/财经/国际/社会） |
| `judge/material.py` | 素材卡判定 + 排序 + 主体配额；`llm_verdicts` 参数接收模型复核结果（keep/drop 两态） |
| `llm/client.py` | 模型客户端：OpenAI 兼容、`temperature=0`、响应缓存、审计落盘、**失败即降级** |
| `llm/context.py` | 把 `CoreAssets/*.md` 按标题切片，按层只注入相关章节（不整篇塞） |
| `llm/prompts.py` | 四套 prompt（L1 查询 / L2 素材 / L3 复核 / D 短语）+ 判定标准注入 |
| `llm/material.py` | L2 素材理解：按搜索查询分组，输出 `EntityHit`；evidence url 必须回链，否则整条作废 |
| `llm/adjudicate.py` | L3 噪声误伤检测：只送「证据 ≥3 条却仍被判噪声」的素材（信号自相矛盾），硬领域也送 |
| `llm/seed_filter.py` | 热点种子：判聚合榜的 100 条热搜「能不能变成素材」（A物料/B玩法/C句子/D主体），再按类型拼查询词 |
| `llm/plan.py` | L1 查询生成：模型召回世界知识 → 产出待验证的查询词，不直接进计划 |
| `llm/phrases.py` | D 标签层：批量判 `phrases_todo.json`，结果写 `phrase_labels.json` |
| `store/lifecycle.py` | 模板词龄（first_seen），旧梗自动降权 |
| `store/rundir.py` | 运行目录、落盘、清理 |
| `report/render.py` | Markdown 日报渲染 |
| `deliver/digest.py` | 把一次运行整理成邮件摘要（截断条数、候选排序） |
| `deliver/email_html.py` | 响应式邮件渲染（表格打底 + 移动端堆叠）+ 纯文本版 |
| `deliver/smtp.py` | SMTP 发送（QQ 邮箱 465/SSL，授权码） |
| `deliver/archive.py` | 把发出邮件的轮次归档到 `data/daily/<日期>/` + 写 `sent.json` |

### 脚本（`scripts/`）

| 脚本 | 职责 | 什么时候用 |
|---|---|---|
| `launch_edge.py` | 启动带调试端口的 Edge（脱离父进程，不被回收）；`--check` 只查端口 | 每次跑批前 |
| `doctor.py` | 环境自检：依赖 / `.env` / 代理 / 9222 / 数据规模 / 模型开关。有问题给退出码 1 | **出任何问题先跑它** |
| `collect_seeds.py` | 抓 tunbang 聚合榜（娱乐榜 + 微博榜）→ 模型判「有没有二创价值」→ 转成抖音查询词，写 `data/input/hot_seeds.json`。失败不阻塞跑批 | 生成计划之前 |
| `plan_queries.py` | 生成当日检索计划 `data/input/query_plan.json`（歌名/挑战/主体/发现/热种五类配额）；挑战类先取热榜、再用上轮结果补齐 | 每次采集前 |
| `collect_douyin.py` | 按 `query_plan.json` 用 CDP 采集 → `data/input/douyin_latest.json`；支持 `--resume` / `--limit` / `--scroll` / `--max-empty` / `--max-shots` / `--page-recycle` / `--spa`（改词优先，实验）/ `--retries` / `--select-timeout` / `--empty-cooldowns`（风控窗口里用「快速浅扫」：`--retries 1 --select-timeout 8 --empty-cooldowns 0`，靠多轮累积而不是单轮重试） | 每次跑批 |
| `probe/probe_search_mode.py` | 对比「整页 goto」与「页内改词」的产出质量：条数、URL 重合率、时效分布、请求数 | 改采集方式前后 |
| `probe/cdp_probe.py` | 单次探测搜索页，排障用 | 采集异常时 |
| `probe/probe_video.py` | 打开视频页读标题/文案 | 分析单条视频时 |
| `probe/probe_sandbase.py` | 探测 SandBase 请求格式与返回字段（填 key 后先跑这个） | 首次接入 / 接口变更 |
| `probe/probe_sandbase_fields.py` | 逐字段核对 SandBase 返回结构（确认 `user_count` 恒 0 等结论） | 复验字段时 |
| `daily_run.py` | 每日总入口：单日锁 → Edge → 计划 → 采集 → 出报 → 发信 |
| `install_task.py` | 注册/查询/删除 Windows 计划任务（登录后 2 分钟 + 09:00 + 11:30 失败重试）；`--save-xml` 生成可手动导入的 XML |
| `migrate_daily.py` | 一次性迁移：把历史 `sandbox/runs/<ts>` 归档到 `data/daily/<日期>/` |
| `review_phrases.py` | 查看/写入「待判定复用短语」的标签（copy / title / noise）；`--auto` 让模型批量判（D 标签层），`--label` 人工改判随时覆盖 | 每轮跑完后 |
| `llm_plan.py` | L1 查询生成：模型召回 → `data/input/query_plan_llm.json`，**不覆盖**当天计划；`--dry` 只看 prompt 规模，`--merge` 才并入 `query_plan.json` | 想试模型召回时 |
| `llm_replay.py` | 离线回放 L2/L3：读已有 `candidates.json`，不联网不采集，产物写 `sandbox/replay/`；`--dry` 估算调用量与成本 | 切换模型层前后对照 |
| `build_keywords.py` | 重建词库：主体 + 主体画像 + 成员归属 + 厂牌 + 别名（可重复运行） | 维护词库时 |
| `probe/probe_music.py` | 探测视频页能否读到 BGM（结论：读不到，留作证据） | 排障 / 复验时 |

### 数据（`data/`、`sandbox/`）

| 路径 | 存什么 | 能否删 |
|---|---|---|
| `data/input/douyin_latest.json` | 最近一次抖音采集结果（逐查询落盘，可 `--resume`） | 可覆盖，别删文件本身 |
| `data/input/query_plan.json` | 当日检索计划（歌名/挑战/主体/发现四类） | 可覆盖，每次采集前重生成 |
| `data/state/bgm_cache.json` | 已采样过的素材→BGM，避免重复花配额 |
| `data/state/phrase_labels.json` | 复用短语的人工/模型标签 |
| `data/state/daily_lock.json` | 单日锁：date/status/attempts，保证一天只跑一次 |
| `data/state/term_lifecycle.json` | 模板词首次出现时间 | ❌ 删了词龄归零 |
| `data/daily/YYYY-MM-DD/` | **已发出的日报归档**：report.md / daily.json / materials.json / meta.json / email.html / sent.json | ❌ 历史资产，永不自动清理 |
| `sandbox/runs/<时间戳>/` | 每次试运行的 report / candidates / materials / `bgm_plan.json` / `phrases_todo.json` / 日志 | ✅ 随时删 |
| `sandbox/diag/<时间戳>/` | 采集失败时的截图 + `diag.json`（URL/标题/正文前 200 字） | ✅ 3 天后自动清 |
| `.edge-cdp/` | Edge 调试配置（**含登录态**，已 gitignore） | ❌ 删了要重新扫码登录 |

### 配置（`config/sources.yaml`）

| 段 | 管什么 |
|---|---|
| `run` | 时区、UA、超时 |
| `sources.douyin_hot` | 热榜开关、条数 |
| `sources.douyin_search` | 输入文件路径、`publish_time`（整页搜索 URL 的时间窗）、`max_queries` / `scroll` / `delay_min~max` / `retries` / 四类 `quota`；内容级时效门槛在 `threshold.max_age_days` |
| `sources.bili_search` | 组合数、每个组合取几条、请求间隔 |
| `threshold` | `min_likes` / `max_age_days` / `percentile` |
| `sources.qq_music` | 榜单 id 列表、`max_rank`、`qps_ms`（换榜间隔，代码真的会读） |
| `sources.bgm_sampler` | 采样开关、预算、每素材条数、缓存 TTL、推断比例 |
| `judge` | `max_hotspots` / `max_cross` / `per_anchor_cap`（按**主体**计） / `recent_days` / `phrase_min_videos` / `ambiguous_anchors` / `repeat_window_days` / `max_repeat_cards` / `repeat_growth_min` |
| `automation` | `daily_time` / `boot_delay_seconds` / `retry_time`（`install_task.py` 生成触发器时读）／`edge_headless`／各步骤超时；`collect_timeout_min` 是**整个采集阶段的预算**，另有 `collect_retries` / `collect_retry_wait_s` |
| `delivery.email` | `enabled`（false 等同 `--no-email`）、SMTP 服务器、附件开关、邮件内各区块条数上限 |
| `cleanup` | 分层清理：`heavy_days` / `log_days` / `diag_days` / `diag_max` / `max_days` |

## 三、环境依赖

```
Python 3.11+（当前 3.13）
httpx / pydantic / PyYAML / python-dotenv / tzdata / playwright
```

```bash
python -m pip install -r requirements.txt
```

> `tzdata` 在 Windows 上必需，否则 `ZoneInfo("Asia/Shanghai")` 报错。

## 四、首次准备（只做一次）

### 4.0 密钥（BGM 采样用）

```bash
cp .env.example .env      # 然后把 SANDBASE_API_KEY 填成真实值
```

- 接口：SandBase `one-video-v3`，免费 100 次/天，$0.001/次
- **不填也能跑**：管线会跳过实际调用，只输出采样计划（`bgm_plan.json`）
- `.env` 已在 `.gitignore` 中，不会入库；`.env.example` 才是模板
- **填完 key 先跑一次探测**，确认请求格式与字段名：

```bash
python scripts/probe/probe_sandbase.py 7704624032147271906
```

它会依次试 4 种常见请求格式，打印 status 和 music 字段。四种都不通时把响应片段发我，按实际结构调整解析。



### 4.1 启动带调试端口的 Edge

```bash
python scripts/launch_edge.py
```

它会用 `--remote-debugging-port=9222 --user-data-dir=.edge-cdp` 启动 Edge。

### 4.2 扫码登录抖音

在这个新开的 Edge 窗口里登录一次。登录态持久化在 `.edge-cdp`，以后不用再扫。

> 为什么不能用你原来的 Edge 配置：Edge 不允许默认配置开调试端口；而直接复制 Cookie 会因应用绑定加密失效。

### 4.3 验证

```bash
python scripts/probe/cdp_probe.py "A2O LITE"
```

输出 `items=16` 之类说明正常；`items=0` 多半是登录失效。

## 五、日常跑批（四步）

```bash
python scripts/launch_edge.py        # 1) 确保 Edge 在跑（已在跑会跳过）
python scripts/plan_queries.py       # 2) 生成当日检索计划（榜单/热榜/上轮结果）
python scripts/collect_douyin.py     # 3) 按计划采集 → data/input/douyin_latest.json
python -m src.main --run             # 4) 出日报 → sandbox/runs/<时间戳>/report.md
```

常用参数：

```bash
python scripts/plan_queries.py                    # 1) 生成检索计划
python scripts/collect_douyin.py                  # 2) 按计划采集（可中断）
python scripts/collect_douyin.py --resume         #    中断后接着跑
python scripts/collect_douyin.py --limit 5        #    只跑前 5 个（调试）

# 没有计划文件时，会回退到「主体轮转 + 模板词 + 榜单歌名」的旧逻辑
python -m src.main --run --prod      # 额外产出 daily.json（归档由 daily_run 在发信后完成）
python -m src.main --clean --days 7  # 清理 7 天前的运行目录
```

## 五·五、邮件投递与每日定时

### 5.5.1 先配邮箱（只做一次）

`.env` 里填三行（`.env` 已 gitignore）：

```
SMTP_USER=你的QQ邮箱
SMTP_PASS=16位授权码     # 不是QQ密码
SMTP_TO=同一个QQ邮箱
```

授权码：`mail.qq.com` → 设置 → 账号与安全 → 开启 POP3/IMAP/SMTP 服务 → 短信验证 → 16 位。
改 QQ 密码会让授权码失效，需要重新生成。

### 5.5.2 手动验证三步

```bash
python -m src.main --run                        # 1) 先出报，确认有数据
python scripts/daily_run.py --email-only --no-email   # 2) 只渲染，不发信
python scripts/daily_run.py --email-only        # 3) 用今天最新一版真发一封
```

⚠️ 第 3 步如果当天**已有正式归档**，且这一轮不是 `official`（例如 `main --run` 的试跑），
`daily_run` 会**跳过归档**并在日志里说明——试发不会覆盖你收到的那份。
确需覆盖加 `--archive`；只想发信不留痕加 `--no-archive`。

### 5.5.2b 试跑不许发信（2026-11-09 起）

原来「先发信、再判断要不要归档」，所以只要跑过一次 `--email-only`，**试跑稿会真的发到邮箱**，
只是不覆盖归档。归档安全 ≠ 邮箱安全，现在加了一道闸：

| 轮次类型（`run.json.kind`） | `--email-only` 行为 |
|---|---|
| `official`（daily_run 自己跑的） | 正常发信 |
| `test`（`python -m src.main --run`） | **拒绝发信**，只把预览写到 `<轮次>/email/preview.html`；确需发送加 `--allow-test` |
| `unknown`（历史轮次补记） | 正常发信（兼容旧目录） |

试跑三不要：

1. 不要用 `daily_run --force` 做试跑——那是正式流程（采集 → 判定 → 发信 → 归档）
2. 不要用 `--email-only` 验证试跑结果——用 `--no-email`，或直接看 `<轮次>/email/preview.html`
3. 不要加 `--archive` 去「覆盖试试」——当天归档只应有一份

试跑只产生 `sandbox/runs/<时间戳>/`，`data/daily/` 与 `prune` 都不会碰它。

第 2 步的产物在 `sandbox/runs/<ts>/email/preview.html`，可直接在浏览器里看手机/PC 效果。

### 5.5.3 注册定时任务

```bash
python scripts/install_task.py            # 注册（登录后 2 分钟 + 09:00 + 11:30 失败重试）
python scripts/install_task.py --query    # 查看状态
python scripts/install_task.py --run-now  # 立刻触发一次
python scripts/install_task.py --remove   # 删除

# 若 schtasks 被安全软件/策略拦截，生成 XML 手动导入：
python scripts/install_task.py --save-xml sandbox/daily_task.xml
# 任务计划程序 → 右侧「导入任务」→ 选择该 XML → 确定
```

> 三条触发器共用 `daily_run.py`，靠单日锁保证只成功一次：
> 登录 +2min / 09:00 / **11:30（仅当前面失败且 attempts<2 时才真正执行）**。

单日锁的判断规则：

| 锁状态 | 行为 |
|---|---|
| 今天没有记录 / 换了一天 | 继续跑 |
| `success` | 直接退出（另一条路径已跑过） |
| `running` 且 < 90 分钟 | 退出（避免并发） |
| `running` 且 ≥ 90 分钟 | 视为僵尸锁，抢占重跑 |
| `failed` 且尝试 < 2 次 | 重试 |
| `failed` 且已达 2 次 | 放弃，等第二天 |

⚠️ **Edge 必须用窗口模式**：实测 `--headless=new` 会被抖音拦到「验证码中间页」，卡片数为 0。
所以 `automation.edge_headless` 默认 `false`，到点会短暂弹出一个 Edge 窗口；若你本来开着，则直接复用。

### 5.5.4 失败了会怎样

- 采集**部分**失败 → 用已采部分出报，邮件底部黄条写明「抖音采集仅覆盖 15/33 个查询（18 个失败或未采完），今日结果可能不完整」
  （覆盖度来自 `douyin_latest.json` 的 `plan_total` 与每个查询的 `error`，不靠解析日志文本）
- 数据源整体失败 → 黄条写「数据源异常：xxx」
- 采集中断（**9222 仍通、页签被关闭**）→ 采集进程内先按 5/10/15 秒重建页签重试；仍失败则退出码 4
- 整轮采集失败 → `daily_run` 按 `collect_retries`（默认 3）重试，每次间隔 60 秒、自动加 `--resume`；
  总时长受 `collect_timeout_min`（90 分钟）约束
- 今天已有部分采集结果（`douyin_latest.json` 是当天的且成功数 < 计划数）→ 自动加 `--resume`
- 登录失效 / Edge 起不来 → 不发空报，改发一封「运行失败」告警邮件，锁记为 `failed`
- 发信失败 → 自动重试一次；仍失败则保留 `report.md`，锁记 `failed`（当天可再触发一次）

### 5.6 日志、诊断与磁盘清理

每轮产生的文件，按体积排：

| 文件 | 体积 | 用途 | 保留策略 |
|---|---|---|---|
| `candidates.json` | 0.5~1.5 MB | 中间产物，只在改判定逻辑时用 | **每个自然日只留最新一轮**，7 天后全删 |
| `report.md` / `materials.json` / `meta.json` / `daily.json` / `run.json` | ~70 KB | 日报、素材卡、正式 JSON、运行标记 | 永久 |
| `report_data.json` / `bgm_plan.json` / `phrases_todo.json` / `run.log` / `judged.json` | ~20 KB | 渲染与排障（`judged.json` 是旧版产物，仅历史目录还有） | **每个自然日只留最新一轮**，7 天后全删 |
| `email/preview.*` | ~47 KB | 仅 `--no-email` 时生成 | 同上 |
| `sandbox/logs/daily-*.log` | 2~3 KB/天 | 跨轮次排障 | 30 天 |
| `sandbox/diag/<时间>/*.jpg` | ~0.25 MB/张，每轮 ≤3 张 | 失败现场 | 3 天，总数 ≤50 |

清理在每次 `--run` 结束时**自动执行**，也可以手动：

```bash
python -m src.main --prune                 # 分层清理（只删重文件，保留日报）
python -m src.main --prune --heavy-days 3  # 改重文件保留天数
python -m src.main --clean --days 30       # 整体删除 30 天前的运行目录
```

两条命令的「年龄」都按**目录名里的日期**算（`20261101-0900`），不看 mtime——
清理本身会刷新 mtime，用 mtime 会导致「7 天前」永远算不出来。

实测效果：72 轮积压从 **16.8 MB → 约 5 MB**，71 份 report 全部保留。

**失败诊断**：采集出现「空结果」或登录失效时，自动截图（JPEG q70）并写 `diag.json`
（含 URL、页面标题、正文前 200 字），存到 `sandbox/diag/<YYYYMMDD-HHMM>/`。
这张图用来区分三种日志里长得一样的情况：**验证码页 / 真空列表 / 页面未渲染**。

**日报归档（试跑 vs 正式）**：

| 位置 | 放什么 | 判据 |
|---|---|---|
| `sandbox/runs/<时间戳>/` | 所有轮次（试跑、预览、正式） | 每跑一次就有一份 |
| `data/daily/<YYYY-MM-DD>/` | **只放真正发出邮件的日报** | `sent.json` 存在 |

归档内容：`report.md`、`daily.json`、`materials.json`、`meta.json`、`email.html`、`email.txt`、`sent.json`
（`sent.json` 记录发送时间、收件人、主题、来自哪一轮）。

每轮目录里的 `run.json` 用于区分来源与状态：

```json
{"kind": "official", "trigger": "schedule", "sent": true, "sent_at": "..."}
```

`kind`：`official`（daily_run 触发）/ `test`（手动 `--run`）。`sent` 只有归档后才会变 `true`。

## 五·六、模型理解层（2026-11-09）

**为什么加这一层**：2026-11-09 的标签扩展实验（`sandbox/experiment/report.md`）证明，
靠「标签共现重组新词」捞不到东西（产出 0.83 倍、负样本无区分度）。模型的长处不在重组
本地共现，而在**带着世界知识做召回**和**读懂一批人在干什么**。

四层各自独立开关，配置在 `config/sources.yaml` 的 `llm` 段：

| 层 | 做什么 | 不做什么 | 开关 |
|---|---|---|---|
| **L1 查询生成** | 针对关注主体召回歌名/别名/名场面/CP/玩法，产出待验证查询词 | 不直接进当天计划；必须联网证伪才留 | `layers.plan.enabled` |
| **L2 素材理解** | 一个搜索查询下的 40 条文案整批读，说出「这里有哪种具名素材」 | 不当判定层：产出只是多一个抽取器，仍走 aggregate → judge | `layers.material.enabled` |
| **L3 噪声误伤检测** | 只送「证据 ≥3 条 **却仍被判噪声**」的素材——信号自相矛盾，多半是词表误伤 | 不送「差 1 条到门槛」的卡（那是证据不足）；不新增/改名素材 | `layers.adjudicate.enabled` |
| **D 文案层** | ① 批量判 `phrases_todo.json` 的标签（长期，写 `phrase_labels.json`）② 每天判一次「这些句子能不能当二创文案用、怎么用」，直接进日报 | 不改素材判定；模型没开/失败就退回原来的「复用短语」栏 | `layers.phrase.enabled` |
| **热点种子** | 判聚合榜热搜「能不能变成素材」（A物料/B玩法/C句子/D主体），再转成抖音查询词；不替代采集链路，只负责「今天该搜什么」 | 抓下来的标题 ≠ 结论，必须回抖音验使用量 | `tunbang.enabled` |

**当前状态（2026-11-09 17:40）**：`llm.enabled: true`，只开了 `material`（L2）。
L1 还没接进流水线（要先联网验证产出质量）；L3 平时不触发（今天的正式数据上是 0 个矛盾样本）；
D 层配置关着，仍走人工 `--label`。**没有一次开满四层**——同时变多个变量，结果好坏都归因不了。

**五条工程约束**（比 prompt 重要）：

1. **evidence 必须回链**：模型给的 url 必须真的在输入集合里，程序校验，对不上整条作废
2. **只做救回/确认，不做晋升**：L3 只有 keep（免掉噪声判定）/ drop（维持丢弃）两票；
   曾经有过 promote，回放实测零价值，已删除
3. **`temperature=0` + prompt 版本号 + 响应缓存**：日报要可复现，否则 `history.py` 的 streak 会失真
4. **失败静默降级**：超时/JSON 非法只跳过该层，09:00 照常发信；但**失败必须写日志**，
   不能悄悄少抽几组素材
5. **模型抽的词进词龄库门槛更高**：≥2 条独立证据 + 置信度 ≥0.7，否则每轮编新词会污染词龄

**名字匹配要容错**：模型会把类型塞进名字里（实测拿到过 `SUPERSTAR（BGM）`）。
L3 匹配走「全等 → 剥括号 → 同名唯一时忽略类型」三级兜底；一条都对不上会写 warning，
不能让裁决静默丢失。

**丢弃留痕**：每次跑批写 `run_dir/dropped.json`（全量丢弃 + 在用条数 + 原因），
日报的「被丢弃」段按在用条数降序、高证据标 ⚠️。
这是发现词表误伤的主要入口——2026-11-09 早上的《SUPERSTAR》（16 条在用被判体育领域）
当时只能靠人翻日志才发现。

**怎么开**（配好 key 之后）：

```bash
# 1. .env 里填 RADAR_LLM_KEY=sk-...
python -m src.llm.client          # 连通性自检，不通就别往下走
python scripts/llm_replay.py --dry   # 估算调用量与成本（不花钱）
python scripts/llm_replay.py         # 拿已有 candidate.json 离线回放，跟规则版对照
```

确认有增益再改 `llm.enabled: true` + 对应 `layers.*.enabled: true`。
**不要一次开三层**——同时变两个变量，结果好坏都归因不了。

**成本**：`deepseek-flash`，一批 L2 约 24 次调用 / 7 万 token，高峰时段约 ¥0.2/天。
审计文件在 `sandbox/runs/<ts>/llm_calls/`，每条调用的耗时、token、原始响应都在。

---

## 五·七、环境自检（`scripts/doctor.py`）

出任何问题先跑这一条，别一层层剥：

```bash
python scripts/doctor.py          # 本地检查，不联网不花钱
python scripts/doctor.py --llm    # 额外测一次模型连通
```

它查：Python 版本、依赖、`.env` 四项密钥（打码显示）、代理变量、9222 端口、
采集文件与检索计划的规模、模型层开关、最近一轮产物。有问题给出退出码 1 和待办清单。

### 三个已经踩过的环境坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `Unexpected status 502 when connecting to 127.0.0.1:9222` | shell 带 `HTTP_PROXY=127.0.0.1:xxxxx`，Playwright 把 localhost 也丢给代理 | `collect_douyin.py` 在 `sync_playwright()` **之前**清代理（驱动在启动时就固定环境，放在 connect 外层无效） |
| Edge 启动了但端口很快不通 | 手动单独跑 `launch_edge.py` 时，进程脱离父进程后被回收 | 手动跑采集必须把「启动 Edge」和「采集」放同一条命令：`bash -c "python scripts/launch_edge.py && python scripts/collect_douyin.py"`。计划任务里由 `daily_run` 常驻，没这个问题 |
| `--limit 3` 之后采集文件只剩 3 个查询 | `--limit` 只裁了本轮跑几个，没参与「保不保留旧记录」的判断 | 已修：`--limit` 现在保留其余旧记录、只重采这 N 个；另外整轮重采前会自动备份到 `data/input/backup/douyin_latest.<日期>.json`（每天一份，留最近 5 份） |

**采集时段**：2026-11-09 实测，09:00 能采到 25/33（76%），16:40–17:05 连续 12 个查询 0 条
（搜索页停在骨架屏、无卡片、无验证码）。**手动补采避开傍晚。**

---

## 六、常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `connect ECONNREFUSED 127.0.0.1:9222` | Edge 没起，或调试端口没开 | `python scripts/launch_edge.py` |
| 采集 `items=0`，页面只有导航 | 抖音登录失效 | 在 `.edge-cdp` 窗口重新扫码 |
| 报 `502 Bad Gateway`（`kngx`） | 抖音网关侧故障，不是验证码、也不是本机代理（2026-11-09 已排除 Clash：Edge 走 `TLB`，代理走 `volc-dcdn`） | 脚本会按 30/90/180 秒退避、连续 3 次冷却 180 秒；仍不恢复就中止，等 `daily_run` 换进程续采 |
| 状态栏 `douyin_search failed` | 输入 JSON 缺失或格式错 | 检查 `data/input/douyin_latest.json` |
| 控制台中文乱码 | PowerShell 编码 | 用 Bash 跑，或设 `PYTHONIOENCODING=utf-8` |
| 素材卡全是 B站老视频 | `publish_time` 没生效 | 检查采集 URL 是否带 `publish_time=7` |

## 七、要调东西时改哪里

| 想改什么 | 改哪 |
|---|---|
| 加/减艺人 | 改 `scripts/build_keywords.py` 的清单 → `python scripts/build_keywords.py` |
| 加/减 CP | **只能手改 `keywords.json` → `锚点层.CP`**：`build_keywords.py` 不生成也不覆盖这一段 |
| 加模板骨架（新的「XX变装」写法） | `keywords.json` → `模式层.模板骨架` |
| 调门槛（嫌少/嫌多） | `config/sources.yaml` → `threshold` |
| 调日报条数 | `config/sources.yaml` → `judge.max_hotspots` |
| 改排版 | `src/report/render.py` |
| 加新数据源 | `src/collectors/` 下新增，`main._collect` 里挂上 |

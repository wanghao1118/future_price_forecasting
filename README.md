# 原油新闻研究台

## 黄金期货工作台

黄金版本位于 `golddesk/`，独立使用 `golddesk/data/gold.sqlite3`，提供上海黄金 `AU2612/AU2701/AU2702` 合约 K 线、十类黄金新闻分类和 UTC+0 时间展示。启动：

```bash
python3 -m golddesk.server --port 8785
```

新闻数据库默认读取原油工作台登记的全部启用信源，可用 `python3 -m golddesk.ingest run --limit-per-source 100 --institution-limit 5` 增量构建；黄金版会按黄金直接主题以及利率/美元/通胀、央行与流动性、地缘避险、ETF/持仓/资金流、原油和白银等价格驱动因素重新筛选候选并写入独立库。黄金版暂不迁移 Experiment Setup、Benchmark、Environment 和策略目录；原油版仍由 `newsdesk/` 运行。完整说明见 [`golddesk/README.md`](golddesk/README.md)。
2026 年历史回溯使用 `python3 -m golddesk.history_crawl --start-date 2026-01-01 --end-date <当前日期>`，从原油工作台的历史公开适配器按日期抓取后再筛选黄金影响因素。

用于查看原油合约图表、整理原油期货相关新闻的本地工作台。按 10 类主题归档新闻，保存事件、来源、预期差和影响渠道，为后续接入指定信息源预留结构。

合约 K 线抓取具体月份合约的真实行情：WTI 和 Brent 来自 Farmbucks 的 NYMEX 延迟行情，上海 SC 来自新浪财经。周线、月线由供应商的真实日线聚合，不使用模拟数据，也不以主力连续序列替代月份合约。

新闻采用“公开来源爬取 → agent 逐条判断相关性和分类 → 校验、去重、入库”的流程。现已接入42个具名来源，包括原8个来源、30家国内券商、摩根士丹利、野村、摩根大通及紫金矿业；国内券商主要采集公开分发的署名研报摘要，海外机构采集官网公开内容；完整清单中的其余来源逐一记录接入状态，不将未接通的来源标为已采集。正式新闻仅纳入具有精确首发时分、已核实来源时区且有原始证据的记录；时间统一存储、展示为 UTC+0，日期筛选也按 UTC 日历日计算。仅日期、未知时区和缺失证据的候选会单独记录跳过数量，不作为新闻入库；旧演示记录已清除。

“信息溯源”区分 `origin_name`（实际发布报告、数据、公告或声明的机构）与 `source_name`（采集信源）。例如财联社转述高盛研报时，来源机构为高盛，采集信源为财联社。逐条 Agent 审核同时提取机构及可逐字核对的 `origin_evidence`；不能确认时不猜机构，而以 `origin_status` 区分原文未明确、多方汇总、无机构发布主体，并写明 `origin_reason`。`origin_reviewed_at` 保留核对时间。人工修正保存在 `origin_method=manual`，后续采集保留该修正；文章中顺带引用的统计机构不自动成为原始发布者。

存量新闻全量溯源使用 `python3 -m newsdesk.review_news_origins --output <本轮目录> --workers 24 --batch-size 80`，通过 Codex CLI `gpt-6.1-sol` 逐条语义审核。目录中保存一致性数据库备份、固定目标清单、按内容指纹保存的逐条审核结果及进度；使用同一目录可续跑，不重复审核已完成且内容不变的记录。并发采集造成的正文变化会重新核对，人工修正不覆盖。该命令只执行这一轮，不注册日常任务。旧规则补齐工具不会覆盖已完成的 Agent 溯源结论。

新闻列表和卡片的首发时间显示为 `YYYY-MM-DD HH:mm:ss UTC+0`；不再在首发时间旁展示预测期限。列表第三列为“信息溯源”，详情和编辑表单也提供独立的来源机构字段。

“信息溯源”的置信等级来自用户提供的《Forecasting数据库权威榜单》的“档位”列（P0/P1/P2）。`newsdesk/source_authority_registry.json` 保存原始行、依据、备注、文件 SHA-256 和明确别名；`origin_authority` 保存匹配结果与单元格出处。匹配以原始发布机构为准，采集信源、正文提及、关联账号不代替原始发布者；中国《金融时报》与英国 Financial Times 不合并。榜单等级是来源级档位，表中的适用范围和备注保留在详情，原有 Agent 判断确信度独立保留。未匹配或多个机构无法统一评级时明示原因。新闻保存和读取均按注册表计算，后续采集、人工修改与重新溯源会同步更新。

覆盖明细位于 `/source-coverage.html`，数据接口 `/api/source-coverage` 区分已有新闻、已接入但暂无有效入库、未覆盖，并分别统计原始发布机构与采集信源的新闻数量。存量字段补齐可执行 `python3 -m newsdesk.backfill_source_authority --output <本轮目录>`，保留数据库备份，不改写正文、分类、首发时间或更新时刻。

## 启动

需要 Python 3.9 或更高版本，无需安装第三方依赖。在项目目录执行：

```bash
python3 newsdesk/server.py --port 8765
```

然后打开 [本地新闻研究台](http://localhost:8765)。服务仅监听本机；在运行终端按 `Ctrl+C` 停止。端口被占用时可改用其他端口，例如 `--port 8766`。

需要在当前用户会话中持续运行两个工作台时，可使用 `python3 -m newsdesk.service`。它通过工作区排他锁避免重复守护，
同时启动原油 `8765` 和黄金 `8785`，任一服务退出后等待 5 秒恢复；向守护进程发送 SIGTERM 会正常停止两个服务。运行状态位于
`newsdesk/data/service/`，当前后台日志为 `supervisor.log`。此方式不安装登录启动项，电脑重启后需重新启动。
AIHub 优先使用启动进程已有的 `AIHUB_API_KEY` / `AIHUB_BASE_URL`；没有现有密钥时，自动读取项目目录之外的 `~/.config/futures-newsdesk/aihub.env`。私有目录须为 `0700`、配置文件须为 `0600` 且属于当前用户；仅支持这两个变量，不执行 shell。服务重启、直接启动和 CLI 模型调用均可复用该永久配置。密钥不进入源码、运行记录或日志。读取已有回测和 Overview 不需要调用模型 API。

## 使用

- `Leaderboard → Experiment Setup` 只记录用户明确指定实验号的实验；不得自动为普通回测或单题测试分配实验号或生成实验卡片。卡片展示冻结的环境协议、Benchmark、检索边界、模型及生成参数、评分口径和当前进度。命名实验必须所有模型全量成功且审计通过后，Overview 和各模型页面才自动采用新结果，显示为 `模型名(Exp01)`。当前未命名回测记录已按用户要求清理；命名实验运行期间仍可在 Overview 中手动选择查看暂定结果。
- `Leaderboard → Overview` 展示同一组冻结数据下所有模型的方向准确率横向排名图；在标题右侧切换 WTI、Brent、SC，各主连独立降序排列。紧凑的两层表头 MAE 表同时对比三个主连的最高价、最低价、收盘涨跌幅误差，未完整回测的指标标为暂定。点击图表或 MAE 表中的模型可进入分析文档和召回记录。
- Overview 的“日频预测”支持选择主连和被预测日期，展示各模型的方向以及最高价、最低价、收盘涨跌幅的预测值与当日绝对误差。看涨和正涨幅标红、看跌和负涨幅标绿，高低点相对前一交易日收盘价着色，相等时保持中性色。标题下展示冻结输入日的开盘、收盘、高低点和收盘到收盘涨跌幅。底部只统计方向一致性：多数方向模型数除以有效预测模型数，失败或缺失预测不计入分母；同时显示看涨、看跌票数和有效模型数。主连选择与上方图表联动，默认显示最近的预测日期。
- 点击左上角“原油新闻研究台”进入 K 线首页。
- “石油合约”下按 WTI、Brent、SC 展开，再点击具体月份合约编号切换图表。
- 图表支持小时、日、周、月周期，显示范围切换、十字线读数、成交量、缩放、平移与恢复视图。
- 图表显示数据来源、行情截止时间和抓取时间，可手动更新行情；延迟行情不标为实时行情。
- 合约 K 线下方每项指标的标题放在上方，历史图占满宽度：价格、持仓量和比率使用折线，成交量和净持仓使用柱形，量价关系按上下两图展示。所有指标共用 K 线当前显示的日期范围，滚轮缩放、平移、显示根数及周期切换均保持联动；悬停或方向键查看具体日期数值。日频指标仅在有真实观测的日期绘图，缺失值留空。
- 左侧“全球新闻”仅为目录分组标题，点击其下的 10 个分类浏览对应新闻，没有单独的“全球新闻”列表页面。
- 使用关键词和筛选条件查找现有记录。
- 点击左侧目录底部的“设置 → 通用设置”，可调整新闻更新及“期货日K和实盘题目更新设置”。期货默认按各市场收盘后的时段日更，配置保存在本地。
- 打开新闻详情，查看或编辑完整记录；不需要的记录可以删除。
- 同一条新闻可属于多个研究分类，在各类别下显示同一条记录；详情保留 agent 的相关性判断、分类依据及原文证据。

新闻保存在 `newsdesk/data/news.sqlite3`，刷新页面或重启服务后仍然保留。源码与本地数据分开存放。

## 新闻采集与 Agent 归档

需要本机已有 `codex` CLI 并已登录。审核将 Codex CLI 作为本地模型调用入口，固定传入 `--model gpt-6.1-sol`，复用已有登录，不读取或复制凭据。每批调用返回每个候选的独立结构化判定；没有通过 agent 审核的内容不会进入正式新闻库。

```bash
python3 -m newsdesk.ingest run
```

也可分步执行，先检查候选，再继续审核归档：

```bash
python3 -m newsdesk.ingest crawl --limit-per-source 100
python3 -m newsdesk.ingest review --candidates newsdesk/data/ingestion/<运行目录>/candidates.jsonl
```

可用 `--sources cls,jin10,jiemian,stcn,ths,aastocks,caixin,goldman_sachs` 指定公开来源，`--batch-size 30 --workers 2` 控制批量与并发。券商/海外机构使用 `--institution-limit 5` 控制每家采集数，原清单机构 ID 在 `source_registry.json` 中；例如 `--sources institution_11,institution_45` 仅采东吴证券和摩根士丹利。财联社还抓取近 48 小时原油/能源专题，同花顺补充期货栏目；财新仅保存公开导语。批量只是调用方式，每条候选都必须有独立的相关性、类别和理由，非相关条目也保留排除记录。没有关键词自动入库或模拟结果兜底。

- 原始响应、候选、抓取报告、审核结果和入库报告在 `newsdesk/data/ingestion/`；未变内容复用审核缓存，失败可以继续处理原候选文件。
- `decisions.json` 同时保留相关与非相关候选的判定。相关条目的证据必须能在抓取文本中逐字核对；分类、字段及时间还会经过数据库校验。
- 保留媒体原始发布时间和本次采集时间；来源没给出可确定时区的首发时间则留空，不拿采集时间替代首发时间。
- 网站提供的公开快讯、摘要和文章正文分别标记内容范围；付费全文不会被摘要冒充。抓取失败、无有效响应或需要账号会在来源报告中体现。
- 重复运行按来源与站内文章 ID 稳定识别记录，缺少站内 ID 时使用原文 URL；相同正文去重，同一原文的内容或栏目 URL 更新保留原 ID。多分类不复制新闻，不为了填满十类补造内容。
- 缺少正文等不完整候选会逐条隔离到本轮 `invalid-candidates.jsonl`，保留原候选、来源、行号和原因，继续采集其他来源并审核有效内容；不会把标题当作正文归档。
- Agent 分类及方向属于研究判断，消息核验状态初始为“待核验”；它不意味着媒体事实已由独立来源确认。
- 复核排除的已有采集记录仍保存在本地，保留原文和审核记录，仅从当前新闻列表隐藏；全量导出或 `/api/news?include_excluded=1` 可检查这些记录。

完整来源登记见 [信源接入清单](docs/source-registry.md)。本地服务默认每 30 分钟更新所有已接入新闻来源，并沿用相同的抓取、Codex CLI 审核、分类和去重流程。尚未接通、缺少账号或发布渠道的来源仍按登记状态保留，不会被标为更新成功。

## Time gate agent

从 Pro60002 迁入的独立新闻时间与答案泄露筛选 agent，名称为 `time gate agent`。它先执行三份答案首次公布日核查，再独立审核候选全文，只返回截止日之前且不含预测结果的原文片段；任一公布日无法核实则返回空新闻。

运行入口为 `python3 -m time_gate_agent`，支持搜索、指定原文审核、时间边界核验和 MCP。使用说明见 [time gate agent](time_gate_agent/README.md)，迁移来源和验证结果见 [迁移测试报告](docs/time-gate-agent-migration.md)。

## 策略评估、环境与 Benchmark

左侧包含 `Leaderboard → GPT6.1 Sol`、`Environment → env01` 和 `Benchmark → 记忆泄漏 / 回测 v1 / 回测 v2 / 实盘`，各级目录可折叠并记住状态。回测 v1 沿用现有每类主连 30 道、共 90 道真实日频 QA；回测 v2 采用相同构建方法，限定已知交易日和预测交易日均在 2026-08-01 至 2026-09-30（含首尾），按整个区间内实际可核验的相邻交易日对计数，允许通过核验的跨月日对。旧九月版冻结快照继续保留。两个回测页均支持全部合约、WTI、Brent、SC 主连切换及题干、代码、日期、题目编号搜索；实盘在当天完整日线可用后，锁定下一交易日预测并等待真实结果。

`env01 v5` 向模型开放新闻与技术指标的 UTC 时分检索：模型提供关键词或指标时间段，`time gate agent` 按冻结预测交易区间起点减 30 分钟计算截止；接口先结合截止检索，再由 time gate 复核召回时间，只返回合格内容。新闻参数为 `search_news(keywords, limit)`，截止由宿主自动注入；技术参数为 `search_indicators(indicator, start_time, end_time?, limit)`，省略终点时使用同一截止。`evaluation agent` 提取四项输出并评分。方向与涨跌幅均按次日收盘相对当日收盘计算：方向统计正确率，涨跌幅、最高价和最低价分别统计 MAE。GPT6.1 Sol 通过 Codex CLI 逐题独立评估，仅开放环境搜索工具，关闭联网搜索及其他工具。逐题保存搜索参数、实际返回正文、公开推理摘要（若有）、最终答案和独立时间审核，页面可查看及下载 JSON；隐藏内部思考无法导出。v2 固定新闻输入与旧月份合约结果保留历史协议，实盘未到期不填成绩。

测试集规则、时间精度和 CLI/API 入口见 [Benchmark 使用说明](docs/benchmark-evaluation.md)；当前接口及时间边界见 [env01 v5 UTC 时分检索](docs/env01-utc-retrieval.md)；历史正文版本限制及旧轮次结果见 [env01 v3 评估记录](docs/env01-active-search.md)。

`env02` 是无搜索与工具接口的日频预测环境，仅保留 `evaluator agent`。模型每题独立会话，只接收题干、已知日线行情和交易区间，直接输出相同四项预测；新闻、技术指标、联网以及其他工具全部关闭，任何工具调用均被拒绝。env02 不配置 time gate agent，不设置检索截止时间，沿用方向正确率和三项 MAE 的评分口径。环境页为 `Environment → env02`；规则见 [env02 无工具环境](docs/env02-no-tools.md)。

`env03` 和 `env04` 复用 env01 v5 的新闻、技术指标和 UTC+0 提前 30 分钟检索协议，分别冻结加载 `search-strategy/s01/SKILL.md` 与 `search-strategy/s02/SKILL.md`。s02 使用 2026 年 6—7 月日线学习 RSI(14) 的正确与错误模式，再在回测 v2 上检验。Exp05 使用 env03/s01，Exp06 使用 env04/s02；两批均固定回测 v2 `qa-6508a948e223740f22dd`，每个模型 124 题。

新版 Benchmark 题干、题目列表与逐题预测均显示“已知／预测”的完整 UTC+0 日期及时分，SC 可展开夜盘、日盘分段；Brent 主连按 ICE 时段换算。新题冻结 `time_windows`；env01 v5 的新闻及技术检索截止为预测区间起点前 30 分钟。时段是交易所安排参考，供应商 OHLC 聚合边界未独立核实；历史题仅补展示，原题、轨迹与成绩保持不变。

### 记忆泄漏 Benchmark

左侧 `Benchmark → 记忆泄漏` 展示从现有正式金融事件库遴选的 100 道单空填空题：按新闻已核验的 UTC+0 首发月份，2026 年 1—10 月各 10 个不同事件、各 10 题。每题展示参考答案、原文连续证据、事件重要性说明、引用新闻及事件库记录；可按月筛选或下载冻结题库 JSON。范围为现有原油及相关宏观金融新闻，重大事件由编辑遴选，不代表全部金融市场的客观前十名。

v2 逐题检查“该次报道新公布的事实”，考查当期数据、决策幅度、金额、规模、期限和事件进展。排除长期人物身份、地理常识、定义以及可以直接利用题干推算的填空；加日期本身不算时效性。每题在出题依据中展示具体的时效性说明，并保留复审与改题记录。

题库与行情预测的回测、实盘数据集分别管理，读取页面不启动模型调用或改变现有评测。构建时校验 100 个不同新闻 ID、每月题量、原文证据、答案及来源首发时间，并冻结正文 SHA256、原始时间证据 SHA256 和题库内容指纹；之后新闻库新增或编辑不会悄悄替换题目。

正式评估只向独立闭卷会话提供题干，关闭联网、检索及外部工具，不提供答案、事件标题、引用或证据。`env00-extend` 由 time gate agent 强制执行闭卷权限，独立的 Codex CLI evaluator 逐题判分，指标为总准确率和分月准确率。`Exp00-extend` 对现有 17 个模型各运行 100 题；Overview 和各模型“记忆泄漏实验记录”展示进度、准确率和逐题结果。答对只能作为模型可能记得历史事实的证据，不能单独证明训练污染或记忆泄漏。题库规则见 [记忆泄漏题库说明](docs/memory-leakage-benchmark.md)，执行及结果口径见 [env00-extend](docs/env00-extend-memory-evaluation.md)。

## 2026 年历史补采

历史补采与最新新闻定时更新分别执行，从公开历史列表、日期归档、机构网站地图和公开检索入口回溯。以下命令补采 2026 年 1 月 1 日至 10 月 30 日；按来源声明的首发日期筛选，缺少日期的内容单独留存。

```bash
python3 -m newsdesk.history_backfill --start-date 2026-01-01 --end-date 2026-10-30 --output newsdesk/data/history-2026 --database newsdesk/data/news.sqlite3
```

默认处理所有已接入来源。三条采集线程按来源轮转；优先月份模式为财联社、同花顺保留一条独立采集线程，其余来源由两条线程继续采集，避免媒体等待整轮券商请求。正文候选分块进入既有 Codex CLI `gpt-6.1-sol` 审核、分类流程，再写入现有新闻库。已存正式记录按来源站内 ID 或原文 URL 保护，避免历史短摘要覆盖完整正文或人工编辑；被复核排除的记录可以重新审核。仅有标题的发现列表不能直接归档。

`newsdesk/data/history-2026/manifest.json` 保存各源覆盖、按月候选分布、真实新增计数及任务状态，`review-ledger.json` 和 `review/` 保存审核断点与报告。重复原命令可续跑，同一输出目录不能同时运行两个实例；`--retry-sources` 重试未完整来源，`--retry-reviews` 重试审核失败候选。模型服务暂不可用时保留队列并退避重试，采集继续进行。

优先补齐 1—9 月时，可在原任务目录续跑以下命令。`monthly-target` 以启动时 10 月正式入库量为基准设置各月覆盖目标；目标只用于统计真实缺口，不改变相关性标准。本次启动时 10 月为 1056 条，初始目标向上取整为 1100 条，启动快照保存在 `priority-baseline.json`。`reference-month` 随本轮日期范围内的 10 月正式入库量提高目标，例如增至 1106 条时每月目标同步为 1200 条；目标不会回落：

```bash
python3 -m newsdesk.history_backfill --start-date 2026-01-01 --end-date 2026-10-30 --output newsdesk/data/history-2026 --database newsdesk/data/news.sqlite3 --priority-months 2026-01,2026-02,2026-03,2026-04,2026-05,2026-06,2026-07,2026-08,2026-09 --monthly-target 1100 --reference-month 2026-10 --review-workers 6 --retry-sources --retry-reviews
```

优先月份和目标保存在原断点中。审核跨来源扫描候选，先填当前正式入库量较少的月份；同月优先分配三次油气专题、两次商品期货栏目、一次其余公开来源的审核位置，其余来源内部按来源轮转，所有候选最终仍需审核。该顺序只依据已核验的上游栏目身份，不根据关键词决定相关性；采集尚未结束时暂缓其余月份。券商日期归档和同花顺按“各月 1 日、各月 2 日……”交错推进，保留旧分页与重试状态；财联社五个公开专题为每个月建立独立时间游标，直接从该月末向前回溯。审核并发可通过 `--review-workers` 设置为 1—24 路；上面的命令使用六路，每条内容仍须 Codex CLI `gpt-6.1-sol` 判断相关性、分类并通过原有校验。`coverage_overview.months` 记录每月 `target` 和 `shortfall`，公共渠道已用尽但目标仍未达成时任务保持 `partial`，明确列出 `target_unmet_months`。

每轮请求数或文章数上限仅用于让出执行权，任务会继续推进断点。财联社除快讯列表外还独立回溯五个油气相关专题。东吴、国信、国金、中信、方正、兴业同时尝试新浪公开分发和原官网或认证专栏，保留每条渠道的覆盖缺口，不用新浪范围冒充机构全部历史。紫金矿业使用官方中英文新闻、媒体聚焦和公开索引。

优先月份模式还回溯财联社13个已验证的宏观、央行、债券、经济数据与期货专题，使用独立的 `sources/cls-macro-topics/`，原五专题的默认范围与断点不变。新通道包括可公开取得的 CFTC WTI 持仓报道；主题仅用于发现，普通公司消息及缺少原油因子的内容仍由 Agent 排除。审核按来源专题或行业编号轮转，防止新增宏观候选一直排在旧队列末尾；审核轮转的通道与专题游标仅在整块审核成功后推进。国内券商另共享 `oil-research/` 中新浪原生油气开采、油服工程、炼化及贸易的公开行业索引，机构署名和发布日期均与原文双重校验，沿用原站内 ID 去重。

同花顺分别保存 `futures.10jqka.com.cn/gzqhyw_list/` 股指期货归档与 `goodsfu.10jqka.com.cn/qhkx_list/` 商品期货快讯日期归档，后者在优先月份模式逐月交错推进。商品栏目为混合品种候选，保留来源真实公开摘要及分钟级发布时间，仍须 Agent 逐条决定是否属于原油相关新闻。独立原始响应、日期分页与待处理队列均可续跑；401/403 或 robots 明确限制时记录访问缺口并停止该渠道请求。

来源没有公开全年接口、历史列表中断、访问受限或日期无法确认时，会保留具体缺口，不把已采集样本标为全年完整。来源覆盖指已记录的公开渠道，不代表机构私有研究门户。财新仅采公开能源栏目摘要；AAStocks 的公开关键词检索有每查询 300 条限制，查询结束不能据此证明覆盖全站全年。Bloomberg 正确的官方按月 sitemap 可发现历史网址，但正文实测403；瑞银当前普通请求403，伯恩斯坦研究入口要求客户登录，这三家未用标题或搜索片段代替正文入库。

## 自动更新与通用设置

服务运行时，新闻默认每 30 分钟更新一次。期货默认为“1 天”，分别在 WTI 纽约 17:35、Brent 伦敦 23:35、SC 上海 15:35 的收盘后时段执行，按各市场当地工作日安排，自动处理美英夏令时；交易所休市或数据尚不完整时保留等待状态。每轮更新全部 9 个月份合约的日线、小时线及 WTI / Brent / SC 三个主连日线，再发布实盘问题；周线和月线由日线聚合。月份行情和主连出题独立并发，避免月份请求延迟出题。任务在服务端运行，关闭浏览器不会中止任务；停止本地服务或电脑休眠时不执行，服务重启后已到期任务补跑一次。

在“期货日K和实盘题目更新设置”中保留 1 天即可使用上述日更时段；选择其他间隔则改为按指定间隔执行，支持 1 分钟至 7 天。只在间隔实际改变时重新安排任务；取消不保存。弹窗显示月份行情、主连日K、实盘题目就绪数量，以及上次结果和下次执行时间。新闻结果显示“本次新增 N 条新闻事件”，并分别记录已有新闻更新、重新收录、重复跳过和内容不完整跳过数量；多个分类中的同一记录只计一次，已有新闻更新不计入新增。

主连更新报告分别记录成功获取响应的数量和完整日K就绪数量，并列出各市场最近完整日线日期。供应商即使返回了今天的运行中行情，也不能掩盖此前完整收盘数据缺失；近端工作日缺口保留为“待核实休市或供应商延迟”，本轮显示部分完成，实盘题目继续等待。正常盘中的上一完整日线、周末及远端历史休市缺口不会因此一直标为异常。

默认日更模式下，若刚收盘市场尚未取得当日完整日线，同一行情任务会每 5 分钟有限重试，最多 12 次，且不超过原收盘更新时间起 1 小时、实盘预测截止时间或当地日切。数据就绪、窗口结束或改用自定义间隔后恢复正常计划；重试状态随设置保存，服务重启或休眠恢复后不会补跑已过期的重试。

正常完成但没有新的相关内容或全为重复内容时，显示“本次新增 0 条新闻事件”，不会标为失败。每类更新同一时间仅运行一轮；抓取或审核真正失败仍会保留原因及已有数据，部分信源失败会与本次新增数量一起显示，避免把抓取失败误解释为“没有新新闻”。

设置和行情任务状态保存在 `newsdesk/data/settings.json`。各主连收盘后的完整日线到齐后，自动发布下一交易日的实盘题目，初始显示待结算；每道仍在提交窗口内的新题随即交给独立后台进程，让 Exp01 的全部基模按原实验参数开始预测。所有结果必须在提交截止前完成并锁定；请求等待、超时及退避受剩余提交时间约束。各模型会话并发推进，共享 API 请求并发上限；旧题尚在运行时也持续检查新题，不等整批结束才处理下一题。同轮使用新完整行情结算此前题目，并为已锁定预测补评分。模型调用不阻塞行情、结算或网页请求，关闭浏览器仍继续执行；服务重启自动检查尚未截止的已发布题并恢复中断批次。

自动预测按“主连、已知交易日、预测交易日”识别同一道题，与刷新生成的数据集编号无关；复用历史批次和锁定预测，不因刷新、重启或可用市场集合变化重复预测。每模型每题只接收一份锁定结果，失败最多补试一次，重启不会重置次数；已过截止的历史题不补交。自动调度状态、各题批次和日志保存在 `newsdesk/data/evaluations/live-batches/`，`GET /api/settings` 的 `live_predictions` 字段可检查运行情况。未完整、过期或存在未核实交易日缺口的行情继续等待；来源全部异常时保留上次实盘题目，回测数据集及历史冻结版本保持原样。首次迁移到日更策略时安排下一个收盘后时段，后续重启保留已保存的计划。

`Benchmark → 实盘` 可在页面内切换主连，按“具体问题、答案、状态、模型预测”四列累计展示每日有效题目，按预测交易日倒序排列。具体问题列只显示主连和预测交易日，点击“查看明细”打开仅含已知时间、预测时间、事件截止及原题全文的弹窗；SC 各交易分段列在对应时间项内，事件截止沿用原题冻结的信息边界。答案显示四项真值或“待结算”，不附高低价单位尾注；状态采用与“已结算”一致的小字号，截止前显示倒计时，截止后显示“提交截止”及完整 UTC+0 时间。每行模型预测列的“查看”打开预测弹窗，沿用 Leaderboard 日频查看器的方向、高低点、收盘涨跌幅、误差和涨跌配色，模型列显示 `模型名(实验号)`，预测进度自动刷新。没有完整真值时不填 MAE，失败及等待状态分别显示。

每个品类的侧栏题数和页面统计均采用累计口径。同一主连、输入日及目标日的重复刷新合并为一道，保留首次截止前有效发布的冻结输入；逾期预览不计正式题量。历史题不可补交预测，读取页面或预测弹窗也不会触发构建、结算或模型调用。

实盘提交截止统一为预测时段开始前 30 分钟，各主连最新未结算题显示每秒更新的倒计时；到期显示“提交截止”及 UTC+0 时间并禁止提交，后端同时检查提交和锁定时间。SC 常规夜盘对应北京时间 20:30 截止。旧冻结文件保持不变，历史题量和已接受锁定记录按原协议保留。现有 WTI 普通工作日行情确认与默认更新时间晚于新截止，因而没有有效提交窗口；具体时序约束见 [Benchmark 使用说明](docs/benchmark-evaluation.md)。

本地接口：

```text
GET /api/settings
PUT /api/settings
POST /api/updates/news
POST /api/updates/contracts
```

设置写入 JSON 字段为 `news_interval_minutes`、`contract_interval_minutes`，单位均为分钟；后者等于 1440 时启用各市场收盘后的日更策略。GET 另返回 `contract_update_schedule` 与任务的 `next_run_at`。手动触发接口用于立即启动对应更新；同类任务运行中返回 409。

## 行情数据与缓存

- 日线历史目标起点固定为 `2026-01-01`。WTI / Brent 主连向 FT 请求此日起的真实日线，SC 使用新浪已有历史。页面“2026 年以来”展示当前可用区间，`history_coverage` 明示请求起点、实际起止日期和记录数。
- WTI、Brent：抓取 Farmbucks 的具体月份合约。日线约有最近 6 个月历史，小时线约有最近 5 天历史，以源实际返回的有效记录为准。
- Farmbucks 的 `YTD` / `1Y` 当前返回周线，不能用作日线补齐；早期月份日线及尚未披露的指标保留缺口。主连和月份合约各自保留来源，不互相替代。
- 上海 SC：抓取新浪财经的具体月份合约，历史范围以源已有数据为准。
- 只保存有效的真实 OHLCV 记录；不补造缺失 K 线或尚未发生时段的数据。
- 行情缓存在 `newsdesk/data/market/`，保留原始响应和抓取时间，缓存有效期为 10 分钟。
- 抓取失败时，仅可使用明确标记为 `stale` 的过期真实缓存；没有可用真实缓存时显示错误，绝不回退到模拟行情。

本地行情接口：

```text
GET /api/market/<contract_id>?period=1d
GET /api/market/<contract_id>?period=1h
GET /api/market/<contract_id>/indicators
GET /api/market/<contract_id>/indicators?history=1&days=30
GET /api/market/<contract_id>/indicators?history=1&start=2026-07-01&end=2026-10-30
```

`contract_id` 使用 `wti-YYYYMM`、`brent-YYYYMM` 或 `sc-YYYYMM`，例如 `wti-202612`。接口返回供应商已有的日线或小时线；周线、月线从真实日线聚合。

指标接口固定使用日线，支持 `?refresh=1`。除价格趋势、成交量、量价关系、持仓量、会员持仓、PCR 和区间位置外，还提供 MA20/MA60、EMA12/EMA26、多周期收益（5/20/60/120/252 日）、RSI14、Williams %R14、ADX14、CCI20、ATR14、布林带、20 日年化波动率和日持仓量变化。价格趋势比较最近 10 根有效日 K 的首尾收盘（9 个价格变化间隔）；量价关系比较相邻两日日 K 的价格和成交量变化；区间位置为 `(最新收盘 - 10 日最低) / (10 日最高 - 10 日最低)`。各指标只使用对应窗口内已经发生的日 K，不足窗口、分母为零或原始值缺失时明确标注，不补造数值。最新日 K 可能尚未收盘。

`history=1` 返回每项指标按日期对齐的 `series`。网页使用 `start/end` 请求当前 K 线可见范围（包含首尾日期），取消独立的指标天数窗口；旧 `days=1–30` 调用保留兼容。价格图显示每日收盘；区间位置图逐日仅使用截至该日的最近 10 根日 K。范围请求先返回本地日线指标，SC 历史 PCR 和会员持仓由最多 6 个共享后台线程读取官方日文件，`history.pending=true` 时网页渐进更新；已关闭交易日的真实缓存可复用，失败和未披露日期正常结束并留空。只绘真实历史点，不将当前快照摊到过去。

env01 v5 的 `search_indicators` 必须由模型指定 `indicator`、`start_time`，可选 `end_time`，时间至少精确到时分且带时区。time gate 统一提供预测交易时段开始前 30 分钟的 UTC+0 截止；省略终点时使用截止，显式越界整次拒绝并提供重试范围。接口先按指标精确可用时间检索，再逐点复核；只有日期或时间无法核实的数据不释放。单次最多返回 366 点，拒绝不消耗 8 次有效查询额度，总尝试另有上限。详见 [UTC 时分检索协议](docs/env01-utc-retrieval.md)。

benchmark 仍保留每品类约 30 道回测题；不可变数据集另外冻结完整 `history_bars`。评测的技术指标按每题输入日截断，并且每个历史点只使用该日及以前的 K 线计算。技术检索凭证同时校验查询参数、返回内容、time gate 记录与冻结快照的 SHA256。

SC 持仓量读取新浪日线原始持仓字段，补充来源为 INE 对应交易日的官方行情文件；同月 SC 期权的持仓量 PCR 按所有行权价的看跌持仓合计除以看涨持仓合计计算。会员数据仅统计交易所披露的对应合约前 20 名多、空持仓，不能解释为全市场净多或净空。根据[交易所公布标准](https://www.shfe.cn/reports/tradedata/dailyandweeklydata/dailyranking/decl/202401/t20240117_331641.html)，原油合约持仓量达到 10 万手时公布排名；当日文件未披露的合约保留空值。

WTI 和 NYMEX Brent 的持仓量读取 Farmbucks 对应月份报价。供应商未注明持仓统计日时显示该限制，不将报价更新时间作为持仓统计日；没有前日值时不计算持仓变化。未取得的月份会员持仓或匹配期权数据明确标为缺失，不以主连、CFTC 品种周报或 ICE Brent 代替。

数据来源：[Farmbucks](https://farmbucks.com/)、[新浪财经](https://finance.sina.com.cn/)。

### 合约图表的 UTC+0 时间口径

合约小时图、抓取时间、K 线读数及技术指标悬停时间统一显示为 UTC+0。日 K 横轴仍保留交易所交易日标签，并额外列出对应交易时段的 UTC 起止日期和时间；日期标签不是 UTC 00:00–24:00 的聚合声明。指标的交易日对应时段也不代表持仓统计的发布时刻。

WTI（CL）及本页面 NYMEX Brent（BZ）的常规时段按 `America/Chicago` 逐日换算，自动处理美国夏令时：2026-10-30 交易日对应 UTC 10-29 22:00 至 10-30 21:00，2026-11-02 的 WTI 常规时段则为 UTC 11-01 23:00 至 11-02 22:00。SC 分别列出夜盘与三个日盘时段；2026-11-02 的夜盘对应 UTC 10-30 13:00–18:30，日盘为 11-02 01:00–02:15、02:30–03:30、05:30–07:00，不能把周五夜盘误标为周日。INE 2026 年节假日及节前取消夜盘按官方日历处理，未核实的特别时段明确标记待确认。

新增的时段元数据是交易所常规安排的换算参考，保留来源及核验状态，不改动真实 OHLCV、原始交易日或日期排序锚点。供应商没有公开确认日 K 聚合边界时，不将常规时段冒充供应商已核验的实际聚合窗口；特殊假期、合约到期或临时调整须以交易所安排为准。规则来源：[CME CL](https://www.cmegroup.com/cn-s/markets/energy/crude-oil/light-sweet-crude.contractSpecs.html)、[CME BZ](https://www.cmegroup.com/cn-s/markets/energy/crude-oil/brent-crude-oil-last-day.contractSpecs.html)、[INE交易时段](https://www.ine.com.cn/eng/reports/calendarholidays/tradingtime/index.html)、[INE休市安排](https://www.ine.cn/services/calenderandholidays/holiday/)。

## DevCloud 内网转发

访问地址：[原油新闻研究台](http://jassonwang-any2.devcloud.woa.com:8765/)。本机仍运行 `127.0.0.1:8765`；通过现有 `tencent-jassonwang` SSH 别名将远端 `127.0.0.1:18765` 反向连接到本机服务。远端 `/data/futures_price_forecasting-forward/public_proxy.py` 由 `futures-newsdesk-forward.service` 提供 `0.0.0.0:8765` 的只读 GET／HEAD 访问，不复制新闻数据库、SSH 密钥或应用凭据。

转发依赖本机服务、网络和 SSH 进程在线；本机隧道的 PID 与日志位于 `newsdesk/data/tencent-forward/`。远端服务可用 `ssh -o BatchMode=yes tencent-jassonwang 'systemctl status futures-newsdesk-forward.service'` 检查，服务配置保存在 `ops/tencent-forward/futures-newsdesk-forward.service`。编辑和运行更新继续通过本机入口执行。

Exp01 新闻与证据分析的独立内网入口为 [新闻与证据分析](http://jassonwang-any2.devcloud.woa.com:8765/analysis/exp01/)。它沿用同一个只读代理，以远端 `127.0.0.1:18778` 单独反向连接本机 `127.0.0.1:4178`，不改变研究台首页。分析页需服务整个 `analysis/exp01-news-evidence/app/dist/` 目录，包括 HTML 和对应哈希快照 JSON；代理仅开放这两种资源，保留筛选参数并压缩较大的 JSON。

分析页本机服务 PID / 日志位于 `analysis/exp01-news-evidence/preview.pid`、`preview.log`；独立 SSH 隧道 PID / 日志位于 `newsdesk/data/tencent-forward/analysis-ssh.pid`、`analysis-ssh.log`。恢复前检查端口及已有进程，避免重复启动。隧道命令为 `ssh -C -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -N -R 127.0.0.1:18778:127.0.0.1:4178 tencent-jassonwang`。它与原研究台转发一样，依赖本机服务和 SSH 连接保持在线。

## yitianyi 公网转发

公网 HTTPS 入口为 [原油新闻研究台](https://u505155-be48-b1b2c1b1.westd.seetacloud.com:8443/) 和 [Exp01 新闻与证据分析](https://u505155-be48-b1b2c1b1.westd.seetacloud.com:8443/analysis/exp01/)。2026-11-04 已验证浏览器中的合约行情、新闻分类和分析页实际数据加载。此入口与 DevCloud 转发同时运行，沿用只读 GET／HEAD 代理；编辑和运行更新继续使用本机入口。

SSH 别名为 `yitianyi`，连接 `root@connect.westd.seetacloud.com:54025`，使用本机独立密钥，可直接运行 `ssh yitianyi`。远端目录为 `/root/autodl-tmp/futures_price_forecasting-forward`，仅部署 `newsdesk/public_proxy.py` 和运行配置。平台 HTTPS 服务映射到容器 `6006`；代理通过两个环回反向端口连接本机：`18765 → 8765` 为研究台，`18778 → 4178` 为分析页。

本机 `com.jasson.futures-newsdesk.yitianyi-forward` launchd 登录启动项保持 SSH 压缩隧道，断线后自动重连；每次连接先检查并启动远端独立 Supervisor 中的代理。启动项位于 `~/Library/LaunchAgents/`，运行脚本与日志位于 `~/.codex/runtime/yitianyi-forward/`，避免 macOS 后台进程访问 Documents 的权限限制。配置源文件见 `ops/yitianyi-forward/`。本机及两个本地服务仍需保持在线；转发不迁移数据库或应用运行任务。

状态检查：

```bash
ssh -o BatchMode=yes yitianyi 'hostname; pwd; /root/miniconda3/bin/supervisorctl -c /root/autodl-tmp/futures_price_forecasting-forward/supervisord.conf status'
launchctl list com.jasson.futures-newsdesk.yitianyi-forward
```

仅重连此入口可执行 `launchctl kickstart -k gui/$(id -u)/com.jasson.futures-newsdesk.yitianyi-forward`。更新代理时，将 `newsdesk/public_proxy.py` 复制到上述远端目录后，用该目录的 Supervisor 配置执行 `restart futures-newsdesk-forward`；更新本机隧道脚本时，先将 `ops/yitianyi-forward/run_tunnel.sh` 同步到 `~/.codex/runtime/yitianyi-forward/run_tunnel.sh`，再重连。

## 项目结构

```text
newsdesk/
  server.py           本地 HTTP 服务与 SQLite / 行情 API
  market_data.py      真实行情抓取、校验与缓存
  news_sources.py     公开快讯抓取与原始响应存档
  research_sources.py 公开机构观点抓取
  additional_sources.py 阿斯达克公开新闻、财新公开导语
  domestic_brokers_a.py 国内券商公开研报摘要（含中信官网）
  domestic_brokers_b.py 国内券商公开研报摘要（含方正官网）
  overseas_sources.py 海外机构官网公开研究和文字稿
  xingye_source.py    兴业公开署名研报、认证策略团队专栏
  ingest.py          Agent 逐条审核、多分类、去重入库
  scheduler.py       持久化更新间隔、后台定时任务与执行状态
  source_registry.json 全量信源及接入状态
  static/             网页文件
    contracts.js      合约目录、真实行情加载与交互 K 线
  data/
    news.sqlite3      首次启动后创建的新闻数据库
    market/           行情原始响应与抓取缓存
docs/
  news-taxonomy.md    分类、字段与归档规则
```

分类和字段说明见 [新闻分类与归档规范](docs/news-taxonomy.md)。采集记录沿用相同分类和字段，并保留来源、首次发布时间和 agent 审核证据。

## 合约展示编号

| 品种 / 交易所 | 2026-12 | 2027-01 | 2027-02 |
| --- | --- | --- | --- |
| WTI / NYMEX | CLZ26 | CLF27 | CLG27 |
| Brent / NYMEX | BZZ26 | BZF27 | BZG27 |
| 上海 SC / INE | SC2612 | SC2701 | SC2702 |

当前共接入 9 个具体月份合约。Brent 使用供应商提供的 NYMEX `BZ` 合约，不再使用 ICE 的 `BRN` 编号。具体月份合约与主力连续序列分别处理；后续更换数据源时，需要核对供应商代码和交易所。
合约参考：[CME WTI](https://www.cmegroup.com/education/courses/introduction-to-energy/introduction-to-crude-oil/wti-overview)、[INE 合约公告](https://www.ine.com.cn/publicnotice/notice/202610/t20261009_833671.html)。


### env00 / Exp00 技术指标对照实验

- env00 只开放 `search_indicators`；题干日线与经 time gate 校验的技术指标是唯一输入。禁止联网、新闻和本地新闻搜索；API 与 MCP 的工具注册和调用权限同时限制。环境只有 time gate agent、evaluator agent 和技术指标接口。
- 时间规则与 env01 v5 相同：UTC 预测交易时段开始前 30 分钟为截止，查询时间先校验、召回记录再复核，越界返回原因供模型修正。
- Exp00 使用 Exp01 的相同 90 道冻结题、技术快照和 17 个模型的参数，新闻快照为空；每模型重新生成 90 份公开分析及四项预测。方向使用收盘方向正确率，高低价与收盘涨跌幅分别使用 MAE。
- Exp00 的 `depends_on_experiment=Exp01`；watch 等待 Exp01 执行、既定补跑和审计结束（completed/partial 且运行终态、锁释放），然后自动开始。audit_failed 不放行。最多三次总执行，仅恢复失败或缺失题。手工执行与 resume 同样校验依赖。
- Experiment Setup 与各模型“回测实验记录”中，Exp01 在 Exp00 上方。展示排序独立于发布时间；新实验全量成功且审计通过后成为默认结果。未指定实验号的测试仍不得创建实验卡片。
- 两批都结束后取消本聊天的定时监控；Exp01 单独结束时继续监督 Exp00。冻结代码分别位于 `newsdesk/data/evaluations/batches/<实验号>/source`，续跑必须使用对应目录并显式指定 evaluations 根目录。

## Oil / FutureX 同端口路径（2026-11-04）

现有两个转发入口增加了路径分流，旧根路径和 `/analysis/exp01/` 继续兼容：

| 入口 | 原油 | FutureX |
| --- | --- | --- |
| jassonwang | http://jassonwang-any2.devcloud.woa.com:8765/oil/ | http://jassonwang-any2.devcloud.woa.com:8765/futurex/ |
| yitianyi 公网 | https://u505155-be48-b1b2c1b1.westd.seetacloud.com:8443/oil/ | https://u505155-be48-b1b2c1b1.westd.seetacloud.com:8443/futurex/ |

两个远端的 `public_proxy.py` 已同步加入 `/oil/` 前缀映射和 `/futurex/` 精确静态资源白名单，FutureX 默认上游为远端环回 `18806`，后端 Host 为 `127.0.0.1:8806`。原油资源和品牌链接采用相对路径；原 `/api/` 只读入口保留。

FutureX 的 `18806 → 本机8806` 使用独立 LaunchAgent：`com.jasson.futurex.tencent-forward` 与 `com.jasson.futurex.yitianyi-forward`，不替换原油及分析页原有SSH连接。安装脚本及运维记录位于本机 `/Users/jasson/Documents/ChatGPT/futurex/ops/forwarding/`，运行日志位于 `~/.codex/runtime/futurex-forward/`。转发依赖本机相应服务与网络在线。

本次仅重启了两台转发服务器的无状态代理。原油后端采集、调度和评估进程继续运行；原有入口及分析页已验证可读。

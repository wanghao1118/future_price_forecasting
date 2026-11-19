# 主连 Benchmark：日频回测与实盘

页面目录为 `Benchmark → 记忆泄漏 / 回测 v1 / 回测 v2 / 实盘`。两个回测页均可选择全部合约或 WTI、Brent、SC 主连，并按题干、合约代码、日期及题目编号搜索。“回测 v1”展示现有主连日频回测，“回测 v2”单独冻结 2026 年 8—9 月范围的同方法题库。模型回答四项结果：次日收盘涨跌方向、次日收盘涨跌幅、次日最高价和最低价。方向使用正确率，其余三项分别使用 MAE。

## 数据与题目

直接读取供应商连续期货序列，不把当前月份合约重新标成主连，也不使用 CFD 替代：

| 品类 | 连续代码 | 数据来源 | 报价单位 |
| --- | --- | --- | --- |
| WTI | CL.1:NYM | [FT / LSEG 近月连续期货](https://markets.ft.com/data/commodities/tearsheet/historical?c=WTI+Crude+Oil) | 美元/桶 |
| Brent | IB.1:IEU | [FT / LSEG ICE 近月连续期货](https://markets.ft.com/data/commodities/tearsheet/historical?c=Brent+Crude+Oil) | 美元/桶 |
| SC | SC0 | [新浪财经国内主力连续](https://finance.sina.com.cn/futures/quotes/SC0.shtml) | 元/桶 |

采用供应商未复权 OHLC，保留换月跳空；它是连续行情序列，不是单个可持有的月份合约，也不将日线 close 冒充官方结算价。公开行情有延迟，源页面及原始响应哈希随数据集保存。

回测 v1 为每品类最近 30 个可核验的相邻完整交易日对，共 90 题；输入当天完整 OHLC，预测下一交易日。周末、已核实的 INE 2026 年休市日及交易所明确没有独立交易日标签的日期正常跳过；未核实的工作日缺口保持跳过，不能把数天后的观测值直接当成下一交易日。缺失、未完成数据不补价格。既有 v1 冻结数据不会因日历修正自动重建。

回测 v2 对两端交易日同时应用 `2026-08-01 <= input_date < target_date <= 2026-09-30`，按整个两月区间内全部可核验的相邻日线对构建，不截成每市场 30 题。8/31→9/1 可以纳入，但仍须通过原有时段核验，不把两个自然月分别截断。交易日按交易所归属日期筛选；例如 SC、WTI 的 9 月 1 日线可包含 UTC 8 月 31 日开始的夜盘或电子盘，这仍属于 9 月 1 日交易日。所有具体时分继续以 UTC+0 展示。缺失工作日、无效日线和未核实特殊时段沿用 v1 的排除规则，排除明细随 v2 manifest 保存；不将排除后的较晚观测重新拼成相邻交易日。

v2 使用独立 `current-backtest-v2.json` 指针及 `profile=v2` 入口。默认回测指针、既有题目、真实标签和模型成绩不受新构建影响。只构建题库不调用模型；后续评估可通过新 `dataset_id` 显式选取该冻结快照。

扩展前的九月版 v2 `qa-be88625e070de629428a` 共 57 题：WTI 18、Brent 19、SC 20，冻结文件继续保留，可按 `dataset_id` 读取。加载时核对该版本自身的冻结范围与哈希，不用扩展后的范围改写历史题目。当前 v2 指针用于新的八至九月题库，v1 与既有模型成绩保持原样。

八至九月首版 `qa-da757aa03d22fe0e369d` 为 116 题，现保留为历史快照。该版将 ICE 总日历中并不影响 Brent 的 8/7、8/31 列为未核实时段，并将 WTI 9/7 误当作应有独立日线的缺口；修正时未改写该快照及其既有成绩。

核验交易所产品日历后的当前 v2 为 `qa-6508a948e223740f22dd`，共 **124 题：WTI 41、Brent 42、SC 41**。按预测交易日归属，八月 60 题、九月 64 题；全部 124 组候选日对通过核验。原 116 题的题干、时间窗口及标签均保持一致，另外恢复 Brent 六组和 WTI 两组。三个市场首题均为 8/3→8/4，末题均为 9/29→9/30。4,815 个旧冻结文件、指针及既有结果文件在构建前后保持一致，不调用模型或改写历史成绩。

Brent 8/7、8/31 按 [ICE 2026 交易安排](https://www.ice.com/publicdocs/Trading_Schedule.pdf) 的 TD 规则保留常规时段；相关调整涉及其他产品，不应过滤 Brent。9/7 的 C 类安排则明确 Brent 于伦敦 18:30（UTC 17:30）提前收盘，按已核实特殊时段构建 9/4→9/7、9/7→9/8，未套用常规收盘时刻。

WTI 9/7 自然日存在交易，但 [CME 官方能源日历](https://www.cmegroup.com/trading-hours.html#tradeDate=2026-09-07) 将 9/6 晚至 9/8 的两段交易归为 **9/8 交易日**。UTC 分段为 **9/6 22:00→9/7 18:30**、**9/7 22:00→9/8 21:00**，中间 PREOPEN 不撮合。供应商缓存、重新请求 9/4—9/9 和单独请求 9/7 的原始响应均没有独立 9/7 行，重解析也未丢行；这与已核实交易日归属一致，无需新增或补造 9/7 OHLC。供应商尚未公开其精确聚合机制，继续保留 `supplier_aggregation_verified=false`。新 manifest 将该日记录为 `no_separate_trade_dates` 和 `trading_date_assignments`，不称全天休市，也不再列为未核实缺口。SC 9/24→9/28 跨已核实中秋休市，继续按原规则保留。

新版题库 `futures-continuous-direction-range-v3-utc-windows` 在题干与 `time_windows` 中冻结已知、预测两个区间，分别包含交易日、UTC 起止时分及分段时段。新闻信息截止时间 `information_cutoff_utc` 等于已知区间的结束时刻。时段未核实的日对不进入新版题库；各品类实际日期与来源以当前冻结数据集为准。

例如 WTI 已知交易日 2026-10-29 对应 **UTC+0 2026-10-28 22:00 至 2026-10-29 21:00**，预测交易日 2026-10-30 对应 **2026-10-29 22:00 至 2026-10-30 21:00**。SC 夜盘与日盘逐段列示，休市间隔不视作连续交易。

这些区间是交易所安排的换算参考；供应商未公开确认 OHLC 的精确聚合边界，元数据保留 `supplier_aggregation_verified=false`。历史题目仅通过只读 API 补充 `display_time_windows`；原题干、模型输入、轨迹及成绩不改写，也不自动重新调用模型。

实盘为“今天完整结果 → 下一交易日预测”，不是自动下单。只有当天日线完整、数据新鲜且未过下一交易时段前截止时间的品类才能提交；尚未就绪的品类单独列为不可用。历史过期输入不会生成实盘成绩。

实盘页面按品类累计展示历史题目，采用具体问题、答案、状态、模型预测四列。累计题仅纳入冻结状态为 `ready` 且发布时间早于该版本原提交截止的有效版本，以主连、输入日和目标日去重，保留首次有效发布的输入及其独立结算真值；重复快照用于关联对应版本的锁定预测。未结算答案显示“待结算”；状态在截止前显示最新题倒计时，截止后显示“提交截止”及 UTC+0 时间。各品类收盘后的日更任务同时结算此前有效题目，并为已锁定预测补评分，恢复运行时也会补结算遗漏的历史题。结算仅使用本轮取得的完整可信行情，不调用模型。

实盘题目已接入期货日K日更任务。在 `设置 → 通用设置 → 期货日K和实盘题目更新设置` 保留默认 1 天，系统按 WTI 纽约 17:35、Brent 伦敦 23:35、SC 上海 15:35 的当地工作日时段更新行情及实盘题目。美英夏令时自动换算；休市、未完成日线和数据过期时等待，不发布可预测标记。更新只切换实盘当前版本，不重建回测或改动已锁定预测；来源全部获取失败或只有过期缓存时保留上次实盘题目。后台本地服务须保持运行，页面关闭不影响执行。

## 标签和指标

设当天收盘价为 C_t，下一交易日收盘价为 C_next：

- `direction`：C_next > C_t 为 up，< 为 down，= 为 flat；准确率为预测与真实方向相等的比例。
- `return_pct`：100 × (C_next / C_t − 1)。1.2 表示上涨 1.2%，不是 0.012；MAE 单位是百分点。
- `high`、`low`：下一交易日完整日线的真实最高、最低价；分别计算 MAE，使用各自原币种/桶。

四个输出必须为结构化、唯一结果；方向须与预测涨跌幅符号一致，预测最高价不能低于最低价，涨跌幅隐含的收盘价须落在预测高低点范围内。失败单独记录，不以零误差或当前价替代。

指标按品类、模式、协议及测试集版本分别统计；没有把 USD 与 CNY 混为一个 MAE。未完成回测显示临时成绩，实盘没有真值时正确率和三个 MAE 都为空。旧月份合约单收盘价协议只保留在历史，不与四项主连成绩混排，旧报告见 [v1 记录](benchmark-evaluation-v1.md)。

## 实盘锁定与结算

新实盘数据集的标签初始为空。模型输出、输入哈希、提交时刻和锁定时刻先写入凭证，随后才允许读取未来真值。锁定后“更新结算”只读取新的完整行情并对原预测评分，不再调用模型或改预测。

期货的下一交易日可能从前一日夜盘开始。提交截止统一为该题 UTC 预测交易时段起点前 **30 分钟**；必须在截止前完成提交及锁定，等于截止时刻也不再接受。SC 常规夜盘 21:00 开始，对应北京时间 20:30 截止；跨日、节后无夜盘以及夏令时变化均以题目实际冻结的预测时段计算。最新未结算题在截止前显示每秒更新的倒计时，到期切换为“提交截止”及 UTC+0 时间，不能再提交。表格列为具体问题、答案、状态、模型预测；未结算答案显示待结算，已结算显示四项真值。

每行“查看”打开按题目精确绑定的模型预测弹窗。模型列为 `模型名(实验号)`，方向、最高价、最低价和收盘涨跌幅沿用日频查看器的数值与红涨绿跌规则。已锁定未结算预测也可查看，未取得该版本真值时不显示误差。接口只读校验运行快照、题目基线与锁定凭证；重复版本的误差按各自输入收盘计算，不移植其他版本真值。

用户明确要求的实盘基模预测可通过 `python3 -m newsdesk.live_prediction_batch` 运行。它复用 Exp01 的 17 模型清单和逐模型生成参数，为当前未截止题目冻结共同新闻与技术快照，结果保存在独立 `live-batches/` 目录和 `mode=live` 运行中，不改变回测实验、题目或既有锁定结果。恢复时只补失败或缺失预测，截止后不派发模型；网络权限、限流或模型错误明确保留为失败。

新题冻结该规则；既有题的页面和当前提交检查通过只读投影采用新截止，冻结文件和哈希保持不变。历史有效发布题量及已经接受的锁定预测，仍按其原冻结协议核对，避免新规则追溯删除历史题或使已锁定预测失效。完整当日日线仍须通过供应商的完整性与更新缓冲检查。现有 WTI 普通工作日最早于纽约 17:30 确认完整行情，默认更新为 17:35，而新截止为 17:30，因此该流程没有有效提交窗口；ICE Brent 在部分夏令时错位时段也存在类似约束。此时保留等待或已截止状态，不提前把未完整行情标为就绪。

ICE Brent 主连 `IB.1:IEU` 与合约页 NYMEX BZ 月份合约使用不同交易所时段。2026-10-30 的 ICE Brent 为 UTC 00:00–22:00；2026-11-02 为 11-01 23:00 至 11-02 23:00。来源：[ICE Brent](https://www.ice.com/products/219/Brent-Crude-Oil-Futures)、[ICE 夏令时过渡安排](https://www.ice.com/publicdocs/futures/Trading_Schedule_Temporary_Trading_Hours_for_DST.pdf)、[ICE 2026 交易日历](https://www.ice.com/publicdocs/Trading_Schedule.pdf)、[CME WTI](https://www.cmegroup.com/education/courses/introduction-to-crude-oil/product-overview/wti-overview)、[INE 交易时间](https://www.ine.com.cn/services/calenderandholidays/tradinghours/)。特殊时段未核实则明确标记，不自行宣称可预测或已结算。

## env01 v5 UTC 时分检索

当前新运行使用 `env01-agent-search-close-direction-return-range-v5-utc-30m`。
新闻与技术指标共用由 time gate 提供的截止：冻结预测交易区间起点前 30 分钟，统一 UTC+0。
模型提供检索条件，接口按精确时间筛选后再复核召回时间。技术参数使用 `start_time` 与可选
`end_time`，省略终点时使用截止，缺少时分或越界整次拒绝。新旧协议分开比较，历史成绩保留。
原 v4 运行仍按已冻结的开始前 2 小时截止核验；新旧成绩分开比较。
完整接口、日线时间依据及题干行情的边界说明见 [env01 v5 UTC 时分检索](env01-utc-retrieval.md)。

## env01 v3 历史主动搜索

历史 v3 环境为 `env01-agent-search-close-direction-return-range-v3`。模型自行选择关键词调用 `env01.search_news`，每次本地召回后由 time gate agent 逐条检查，只将合格新闻正文返回模型。每题最多八次搜索，必须有至少一次真实且经回执验证的成功调用才允许计分；合法空结果也保留。联网搜索和其他模型工具关闭。

新版 UTC 区间题按 `information_cutoff_utc` 精确过滤新闻首发及来源修订时间，只有日期、没有可核实带时区时分的记录不放行。历史题仍按其冻结的日期规则运行；技术指标仍按交易日查询，只能使用输入交易日及以前的冻结观测。

逐题轨迹包含实际参数、模型收到的响应正文、公开消息、可见推理摘要（若模型返回）及最终答案；宿主时间审核摘要分开列示。页面 `Leaderboard → GPT6.1 Sol → 逐题预测与结算 → 查看交互轨迹` 可查看或下载 JSON。隐藏内部思考无法导出。主动搜索结果与旧 v2 分别标记，具体协议、审计、新闻覆盖和本轮成绩见 [env01 v3 评估记录](env01-active-search.md)。

## env01 v2 历史运行记录

保留两个 agent：离线 time gate agent 仅从已爬取、按对应交易所时区不晚于输入日且早于目标日的新闻中选择；evaluation agent 解析四项输出、校验后按真实标签评分。日期未知、演示、审核排除或无正文记录不放行。

历史 v2 中，GPT6.1 Sol 使用 Codex CLI / gpt-6.1-sol / medium，每题独立临时会话，关闭全部工具并预先输入固定新闻。模型不接收真实标签或其他日期的问题。每轮冻结新闻快照，最多八篇，每篇最多 4000 字符；截断和历史网页版本未验证标记保留。

当前数据集：

- 回测：`qa-bc2378bb8feb0b2f1f29`，90 题。
- 实盘：`qa-b9a31343bf0af167ba26`，3 个品类预览，其中 SC 1 个就绪。
- 回测运行：`20261029T120203Z-ffe3eaa6`。
- 实盘运行：`20261029T120203Z-7f437a07`，SC 预测已锁定，等待真实结果。

v2 固定新闻输入回测 **90/90 题成功，0 失败**，三项数值误差与方向标记均已独立重算一致：

| 品类（各30题） | 方向正确率 | 收盘涨跌幅 MAE（百分点） | 最高价 MAE | 最低价 MAE |
| --- | ---: | ---: | ---: | ---: |
| WTI | 56.67% | 2.1122 | 1.7140 美元/桶 | 1.2590 美元/桶 |
| Brent | 40.00% | 2.0282 | 1.7440 美元/桶 | 1.2860 美元/桶 |
| SC | 60.00% | 2.6848 | 16.8633 元/桶 | 15.0033 元/桶 |

上述 SC 实盘运行尚无真实答案，方向正确率和三项 MAE 均为空；实际执行“更新结算”已验证不会重新调用模型或改变已锁定预测，仍保持待结算。上述 v2 回测与实盘轨迹没有工具调用；v3 使用主动环境搜索。

公开 API：

```text
GET  /api/research
GET  /api/benchmark?mode=backtest|live
GET  /api/benchmark/questions?family=WTI&mode=backtest|live
GET  /api/benchmark?mode=backtest&profile=v2
GET  /api/benchmark/questions?mode=backtest&profile=v2
GET  /api/benchmark?mode=live&history=1
GET  /api/benchmark/questions?family=WTI&mode=live&history=1
GET  /api/benchmark?mode=live&kind=predictions&dataset_id=<version>&case_id=<question>
POST /api/benchmark/build       {"days":30,"mode":"backtest"}
POST /api/benchmark/build       {"mode":"backtest","profile":"v2"}
POST /api/evaluations           {"mode":"backtest","workers":6}
POST /api/evaluations           {"mode":"live","family":"SC","workers":1}
POST /api/evaluations/<run_id>/settle {"refresh":true}
GET  /api/evaluations/<run_id>/cases/<case_id>/trace
GET  /api/evaluations/<run_id>/cases/<case_id>/trace?download=1
```

CLI：

```bash
python3 -m newsdesk.evaluation run --mode backtest --workers 6
python3 -m newsdesk.evaluation run --mode live --family SC --workers 1
python3 -m newsdesk.evaluation settle --run-id RUN_ID
```

数据和运行记录位于 `newsdesk/data/benchmark/`、`newsdesk/data/continuous-market/`、`newsdesk/data/evaluations/`，沿用 Git 忽略和私有标签存储。回测/实盘使用独立当前版本指针，旧版本不删除；实盘未来真值保存在独立结算记录，不改已冻结题目。

验证：newsdesk 152 项、原 time gate 17 项自动测试通过，合计 169 项；另通过前端模式与指标 VM 验证、实际 API 验证和浏览器检查。

## 已清理的未命名回测

2026-11-05 按用户要求删除了 2026-11-01 的未命名全量回测及两次未命名单题测试，共 19 个运行目录；对应旧批次的状态、审计和日志文件也已删除。这些历史结果不再作为 Leaderboard 默认结果，后续可在 Overview 手动选择尚在运行或部分完成的命名实验查看暂定结果。

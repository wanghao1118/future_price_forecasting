# 黄金期货新闻研究台

`golddesk` 是独立于原油工作台的黄金期货工作台。当前只迁移了合约行情、UTC+0 交易时段展示、新闻分类/检索/编辑和独立新闻数据库；Experiment Setup、Benchmark、Environment 与策略目录暂不迁移。

## 启动

在仓库根目录运行：

```bash
python3 -m golddesk.server --port 8785
```

打开 <http://localhost:8785/>。不要直接双击 `golddesk/static/index.html`；该文件会自动跳转到 HTTP 地址。默认数据库是 `golddesk/data/gold.sqlite3`，也可通过 `--database` 指定其他 SQLite 文件。`--seed-demo` 只用于界面检查，不应混入正式数据库。

需要让黄金工作台在当前用户会话中持续运行并在服务退出后自动重启时，使用：

```sh
python3 -m golddesk.service
```

行情当前使用新浪财经公开的上海期货交易所黄金月份合约：

- `AU2612`（2026-12）
- `AU2701`（2027-01）
- `AU2702`（2027-02）

日线、小时线、周线和月线均保留供应商真实响应；周线/月线由同一月份合约的真实日线聚合，不用连续合约或模拟价格补齐。行情缓存位于 `golddesk/data/market/`。

合约指标接口沿用原油工作台的日线计算入口，提供价格趋势、成交量、量价关系、持仓量、区间位置、MA20/MA60、EMA12/EMA26、多周期收益、RSI14、Williams %R14、ADX14、CCI20、ATR14、布林带、20 日年化波动率和日持仓量变化。所有曲线按具体月份合约计算，窗口不足或原始字段缺失时保留空值。

## 新闻数据库

执行以下命令重新抓取公开信源并增量写入独立数据库。默认会读取原油工作台 `newsdesk/source_registry.json` 中全部启用信源，使用原油工作台已有的快讯、财经媒体、券商、海外机构和公开账号适配器；黄金版在候选进入本库前重新做黄金相关性筛选和分类。筛选同时覆盖黄金直接主题，以及美联储/利率/美元/通胀、央行与流动性、地缘冲突与避险、ETF/持仓/资金流、原油和白银等黄金价格驱动因素。

```bash
python3 -m golddesk.ingest run --limit-per-source 100 --institution-limit 5
```

如需只重用某一轮已经完成的原油信源抓取，可传入该轮的 `candidates.jsonl`，不会重复请求信源：

```bash
python3 -m golddesk.ingest run --input-candidates golddesk/data/ingestion/<批次>/candidates.jsonl
```

要回溯抓取 2026 年全年已经发生的新闻，使用原油工作台的历史公开适配器。默认范围是 `2026-01-01` 到当前 Asia/Shanghai 日期，支持断点续跑；未来日期不会被当作已采集：

```bash
python3 -m golddesk.history_crawl --start-date 2026-01-01 --end-date 2026-11-16
```

该命令默认回溯金十、财联社、同花顺、界面、证券时报、AAStocks 和财新，再将候选交给黄金主题/价格驱动筛选。原始响应、断点和覆盖报告保存在 `golddesk/data/history-<结束日期>/`。

采集结果位于 `golddesk/data/ingestion/<UTC 批次>/`，包括原始响应、候选、审核报告和跳过原因。另加入 Google News RSS 的中英文黄金期货发现流；RSS 记录保留精确 RFC-822 发布时间、原始 URL 和内容范围。入库记录的 `published_at`、`collected_at`、`updated_at` 和时间核验字段统一转成 UTC+0；缺少精确时分、时区或原始证据的候选会被跳过。

当前随仓库提供的 `golddesk/data/gold.sqlite3` 是公开候选的初始快照，包含迁移后的原油信源与 Google News RSS 返回的近期和部分历史条目。它们的 `verification` 为“待核验”，规则初筛记录会明确标为 `golddesk-rule-review-v1`，不把关键词命中当成事实核验或投资结论；后续可在详情页继续人工编辑或替换为更严格的模型审核结果。

新闻分类采用黄金主题的十类词表：生产与矿产供应、实物消费与工业需求、库存仓单、精炼回收、地缘避险、贸易运输、宏观金融、央行官方储备、期货资金交易、天气灾害与矿区风险。所有记录关联黄金期货品种 `AU`。

## 与原油工作台的边界

黄金工作台不读取或复制 `newsdesk/data/benchmark`、评估运行、实验配置和 `search-strategy`。原油工作台继续使用原来的 `newsdesk/server.py` 与 `newsdesk/data/news.sqlite3`。

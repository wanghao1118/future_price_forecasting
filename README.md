# 原油期货新闻研究台

这是一个只面向 WTI、Brent 和上海 SC 原油期货的研究台。项目包含原油行情、原油相关新闻采集与归档、来源管理、UTC+0 时间处理、技术指标和只读网页服务。

## 数据

正式新闻库位于 `newsdesk/data/news.sqlite3`，本仓库通过 Git LFS 保存这份数据库。克隆仓库后执行 `git lfs pull` 获取完整文件。其他抓取过程文件、运行日志、评估快照和临时备份不属于提交内容。

新闻按十类原油因子归档，包含供应、需求、库存、炼厂、地缘政治、运输贸易、宏观金融、能源政策、期货资金与天气灾害。每条记录保存来源、首发时间、原文范围、UTC 时间、原油相关性、分类依据和信息溯源字段。关键词命中不会直接写入正式新闻库，候选需要经过逐条审核、证据校验、去重和时间核验。

分类和字段规则见 [`docs/news-taxonomy.md`](docs/news-taxonomy.md)，来源接入状态见 [`docs/source-registry.md`](docs/source-registry.md)。

## 启动

项目使用 Python 3.9 或更高版本，不需要第三方 Python 依赖。在仓库根目录运行：

```bash
python3 newsdesk/server.py --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。服务提供原油合约页面、新闻列表、新闻详情、来源覆盖和只读数据接口。

需要在当前用户会话中自动重启原油服务时运行：

```bash
python3 -m newsdesk.service
```

AIHub 密钥只从进程环境变量或项目目录之外的 `~/.config/futures-newsdesk/aihub.env` 读取，不写入源码、新闻库或日志。

## 新闻采集

增量采集并审核公开来源：

```bash
python3 -m newsdesk.ingest run
```

分步运行：

```bash
python3 -m newsdesk.ingest crawl --limit-per-source 100
python3 -m newsdesk.ingest review --candidates newsdesk/data/ingestion/<运行目录>/candidates.jsonl
```

2026 年历史公开内容回溯：

```bash
python3 -m newsdesk.history_backfill \
  --start-date 2026-01-01 \
  --end-date 2026-10-30 \
  --output newsdesk/data/history-2026 \
  --database newsdesk/data/news.sqlite3
```

原始响应、候选、审核过程和报告默认保存在本地 `newsdesk/data/`，不会随项目提交。运行过程中产生的文件可以在保留新闻库的前提下清理。

## 行情和指标

WTI 和 Brent 使用具体月份合约的公开延迟行情，SC 使用上海原油公开行情。项目保留真实 OHLCV 和供应商时间信息，不用模拟价格补齐缺口。周线和月线从真实日线聚合。

主要接口：

```text
GET /api/market/contracts
GET /api/market/<contract_id>?period=1d
GET /api/market/<contract_id>?period=1h
GET /api/market/<contract_id>/indicators
GET /api/news
GET /api/config
```

技术指标包括均线、EMA、多周期收益、RSI、Williams %R、ADX、CCI、ATR、布林带、年化波动率、区间位置和持仓量变化。缺少真实观测时保留空值，不补造数据。

## DevCloud 转发

内网入口为 `http://jassonwang-any2.devcloud.woa.com:8765/oil/`。本机 `8765` 通过 `tencent-jassonwang` SSH 别名反向连接到远端 `127.0.0.1:18765`，远端只读代理由 `futures-newsdesk-forward.service` 提供。

公网入口为 `https://u505155-be48-b1b2c1b1.westd.seetacloud.com:8443/oil/`。公网代理只开放原油页面和只读 GET／HEAD 请求，不复制 SSH 密钥、AIHub 配置或其他本地凭据。

转发配置位于 [`ops/`](ops/)。转发依赖本机原油服务、网络和 SSH 连接保持在线。

## 项目结构

```text
newsdesk/
  server.py                 原油网页与 HTTP API
  market_data.py            原油月份合约行情
  market_indicators.py      原油技术指标
  news_sources.py           公开新闻采集
  research_sources.py       公开机构内容采集
  ingest.py                 候选审核、分类、去重和入库
  source_registry.json      原油信源登记
  static/                   原油网页资源
  data/news.sqlite3         原油新闻数据库

docs/
  news-taxonomy.md          原油新闻分类与字段规范
  source-registry.md        原油信源接入清单

ops/
  原油网页转发和远端只读代理配置
```

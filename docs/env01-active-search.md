# env01 v3：模型主动搜索与三主连评估

已完成本地环境改造及真实 Codex CLI 重评。WTI、Brent、SC 各 30 题，共 **90/90 成功，0 失败**。使用同一冻结主连日频回测集，未重新构建行情或真实标签。

- 运行：`20261029T155907Z-ba7a60c7`
- 测试集：`qa-bc2378bb8feb0b2f1f29`
- 模型：`gpt-6.1-sol`，Codex CLI，medium，每题独立会话。
- 协议：`env01-agent-search-close-direction-return-range-v3`
- 冻结新闻：628 条，SHA256 `95be9c4fd255ae418abf966f256a4ccc69c585e00397c969198da0bca53fdc12`。

## 成绩

方向和涨跌幅均以次日收盘相对当日收盘结算。涨跌幅误差使用百分点；高低点误差使用各品类原币种。以下数值已从冻结标签独立重算，逐题与汇总一致。

| 主连 | 题数 | 方向正确率 | 收盘涨跌幅 MAE（百分点） | 最高价 MAE | 最低价 MAE |
| --- | ---: | ---: | ---: | ---: | ---: |
| WTI | 30 | 56.67% | 2.1088 | 1.7063 美元/桶 | 1.2500 美元/桶 |
| Brent | 30 | 33.33% | 2.1089 | 1.8280 美元/桶 | 1.4120 美元/桶 |
| SC | 30 | 56.67% | 2.7498 | 17.4133 元/桶 | 15.9367 元/桶 |

## 搜索与时间门限

模型可调用 `env01.search_news({"keywords":"原油 OPEC 库存","limit":5})`。关键词由模型选择，可继续搜索；每题最多八次有效调用，每次最多八篇。唯一模型工具为这一环境搜索，联网搜索、shell、执行、浏览器、应用、插件及多 agent 关闭。

环境只搜索本轮冻结的已爬取新闻。每次召回的全部候选先逐条经过 time gate，不合格内容不返回模型，也不会因未来新闻排名靠前而挤掉合格候选。未知日期、非正式抓取、演示、排除记录、无正文、明确事后修订记录拒绝；按问题对应的交易所时区要求发布日期不晚于输入日，且严格早于目标日。模型不能修改日期、快照路径或题目。

正文为保存原文或明确标记的原文前缀，每篇最多 4000 字符、每次总正文最多 10000 字符、响应最多 45000 UTF-8 字节。空结果保留为 `no_results`，不伪造新闻。

| 主连 | 实际搜索次数 | 获得合格新闻的题数 | 返回文章次数（跨题可重复） |
| --- | ---: | ---: | ---: |
| WTI | 33 | 18 / 30 | 58 |
| Brent | 30 | 13 / 30 | 48 |
| SC | 33 | 28 / 30 | 114 |

共 **96 次真实搜索**：60 次返回新闻，36 次空结果；**59/90 题获得新闻，31 题仅空结果**。共返回 220 次文章、28 篇唯一新闻。全部 96 次已绑定原生 CLI 的 `mcp_tool_call`、模型参数、环境响应哈希和审计回执；缺少成功主动搜索的题不能计分。

审计重新核验全部 12828 条召回候选及 time gate 决策、原文前缀和逐文件 SHA256，未发现不一致。审计执行器只读取原有记录，没有重新调用模型。

## 查看和保存记录

页面：`Leaderboard → GPT6.1 Sol → 逐题预测与结算 → 展开题目 → 查看交互轨迹`。每题可下载 JSON，轨迹按需读取；自动刷新保留展开状态和正文滚动位置。旧 v2 固定新闻输入结果保留原协议标记。

保存范围包括模型公开消息、每次接口参数、模型实际收到的新闻正文、可见推理摘要及最终答案。39 题返回可见推理摘要，51 题未返回，页面明确标记缺失。每题最终 JSON 均有模型公开理由。隐藏内部思考无法导出，也未推测或补写；OpenAI 对 [reasoning summaries 的说明](https://developers.openai.com/api/docs/guides/reasoning)区分了可提供的摘要与不公开的原始推理。

宿主 time gate 审核单独展示，不把拒绝新闻的正文、标题或 ID 混进模型可见轨迹。完整原始召回保存在本机私有审计目录，未作为模型输入。

运行文件位于 `newsdesk/data/evaluations/runs/20261029T155907Z-ba7a60c7/`：

- `run.json`：协议、冻结快照哈希及全部指标。
- `dataset.json`、`news-snapshot.json`：原冻结数据。
- `cases/<case_id>/result.json`：逐题预测及得分。
- `cases/<case_id>/<attempt>/input.json`：当时传入的公共输入对象。
- `cases/<case_id>/<attempt>/model/cli-events.jsonl`：过滤凭证及不公开字段后的原生公开事件。
- `cases/<case_id>/<attempt>/model/conversation.json`：按顺序整理的公开交互。
- `cases/<case_id>/<attempt>/model/search/<call_id>/`：request、raw-recall、time-gate、response 和 receipt JSON。
- `audit-search.json`：全部搜索和正文的独立复核；`validation-scores.json`：独立重算成绩。

API：

```text
GET /api/evaluations/20261029T155907Z-ba7a60c7
GET /api/evaluations/<run_id>/cases/<case_id>/trace
GET /api/evaluations/<run_id>/cases/<case_id>/trace?download=1
```

再次运行同一数据集：

```bash
python3 -m newsdesk.evaluation run --mode backtest --dataset-id qa-bc2378bb8feb0b2f1f29 --workers 6
```

## 数据限制与验证

历史正文版本没有独立核验。冻结新闻都在 2026-10-28 至 10-29 抓取；220 次实际文章返回中，214 次带 `historical_version_unverified=true`。原始发布日期合规不能证明正文与预测当日网页版本完全相同。较早历史日期的相关新闻稀疏，因此须同时看新闻覆盖率，不能把所有题描述成实际用过新闻。

本轮重评只针对回测。原 v2 SC 实盘锁定预测继续保留，未重新生成或提前填写未来成绩。原主连行情来源缺口、换月及日历约束见 [Benchmark 说明](benchmark-evaluation.md)。

验证完成：newsdesk 173 项、原 time gate 17 项，共 **190 项测试通过**；另通过两组前端 VM 检查（包括真实轨迹、下载、转义和刷新状态）、实际 API 下载一致性与浏览器检查。

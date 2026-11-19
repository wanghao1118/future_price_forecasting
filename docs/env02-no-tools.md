# env02：无搜索与工具的预测环境

`env02` 使用现有主连日频 Benchmark，仅保留 **evaluator agent**。待测模型每题开启独立会话，只接收题干、已知日线行情，以及已知和预测交易区间，直接给出下一交易日的收盘方向、收盘涨跌幅、最高价和最低价。

环境协议为 `env02-no-tools-close-direction-return-range-v1`，工具模式为 `closed_book_no_tools`。模型侧不注册任何接口：新闻、本地新闻、技术指标、联网搜索，以及浏览器、文件、shell、应用、插件、外部记忆和多 Agent 工具均关闭。若模型响应仍包含工具调用，宿主拒绝该次结果，不执行调用。

env02 不配置 time gate agent，也没有检索截止时间。题干继续标明 UTC+0 的已知和预测区间；真实标签只由评估侧保存，在预测锁定前不提供给模型。行情题干输入范围沿用 Benchmark 规则。

evaluator agent 在模型答案锁定后提取并校验四项预测：收盘方向按下一交易日收盘相对已知日收盘结算，统计正确率；收盘涨跌幅、最高价和最低价分别计算 MAE，其中涨跌幅单位为百分点。WTI、Brent、SC 分开报告，高低价保持各自币种。实盘缺少完整真实行情时先保存预测，等待结算。

`Environment → env02` 展示单 Agent、输入范围、无接口权限及评分规则。逐题记录保留实际模型输入、公开分析文档、最终答案、权限校验与评分；隐藏内部思考不导出。env02 使用独立环境协议，与 env00、env01 的运行分开识别。新建环境本身不创建实验或启动模型调用。

后续可使用现有评估入口选择 `environment_id: "env02"`，或通过 CLI 指定环境：

```bash
python3 -m newsdesk.evaluation run --environment env02 --strategy-id aihub-deepseek-v4-pro --mode backtest --one-day --family WTI
```

AIHub Chat 与 Responses 的实际请求均固定 `tools: []`、`tool_choice: "none"`；Codex CLI 使用独立临时会话、空 MCP 配置，并关闭工具发现和执行。宿主权限检查不调用 time gate agent。新闻与技术快照为空，逐次请求、可见响应、CLI 配置、预测锁和评分可由独立审计重新核对；格式修复同样保持零工具且不向模型提供真实答案。

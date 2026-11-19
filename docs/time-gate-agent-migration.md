# time gate agent 迁移验收

验收日期：2026-10-29。结果：迁移完成，自动回归、真实模型/公开网页调用及本地 MCP 通路验证通过。

## 来源与本地入口

- 已通过 SSH 别名 `Pro60002` 实际连接并确认主机 `autodl-container-xacalgm6rb-61488251`，项目目录 `/root/autodl-tmp/wh/moonsword`。
- 迁入最新版 `experiment05_choice_news_guard.py` 和 `prepare_experiment05_choice_boundaries.py`；采用 `experiment05_2_choice_verified_20261027_01` 的规则。原始及迁入 SHA-256 存于 [迁移清单](../time_gate_agent/migration.json)。
- 本地包：`time_gate_agent/`；运行入口：`python3 -m time_gate_agent`；MCP 服务名称：`time gate agent`。
- 保留三份独立首次公布日核查、任一未核实则关闭新闻、整篇文章答案泄露检查、精确原文引用和私有审计。补齐可在本机运行的宿主编排和 Codex CLI 适配。
- 本机验证环境：Python 3.9.6，Codex CLI `0.165.0-alpha.1.2`，模型 `gpt-6.1-sol`，推理强度 `medium`；复用已有本机登录，没有迁移服务器凭据。

## 自动测试

| 验证 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s time_gate_agent/tests -v` | 17/17 通过 |
| `python3 -m unittest discover -s newsdesk/tests -v` | 原项目 100/100 通过 |
| Python 3.9 语法检查 | 通过 |
| 本地 HTTP 有效 token / 缺少 token | 正常返回 / HTTP 403 |
| stdio MCP 初始化、工具清单、工具调用桥接 | 通过，服务名正确 |
| 审计目录与文件权限、迁入文件 SHA-256 | 通过 |

17 项新回归覆盖：三份核查取最早日，未核实及模型异常时关闭新闻，来源日期与任务绑定，凭证被篡改时失效，强制搜索截止日，正常事前文章放行，答案/回顾/当天文章/后改版本/编造日期/改写引用拒绝，以及私有内容不进入公开响应。

## 真实模型与网页测试

使用 [历史测试问题](../time_gate_agent/examples/fed-january-2025.json)，分别运行 `open` 和 `search`，所有核查及文章判断均实际调用本机 Codex CLI；来源页由宿主真实抓取。

两轮中，三份独立日期审核均使用公开网页搜索，并各自给出有原文日期证据的首次公布日 `2025-01-29`；宿主重新抓取核验通过。公共策略排除当天，最后允许日为 `2025-01-28`。

| 路径 | 实测结果 |
| --- | --- |
| `open` 审核 2024-12-18 官方声明 | 放行，返回经核对的原文片段 |
| `open` 审核 2025-01-29 官方结果声明 | 拒绝，公开结果不含该文章 |
| `search` 查询含 `before:2029-01-01` | 宿主改为 `before:2025-01-29`，实际发现 5 个 URL |
| 搜索后独立全文审核 | 2 篇通过；另 3 个 URL 无法取得合格全文，保留私有拒绝记录 |

`open` 最终返回 1 篇合格来源，`search` 最终返回 2 篇合格来源，均为 `status=ok`。额外断言确认返回来源日期均严格早于边界，且不含目标结果文章。

可复查的本地记录（私有、已被 Git 忽略）：

- 指定原文测试：`newsdesk/data/time-gate/99a463d9c3f447baae981e0767c55055/`
- 搜索测试：`newsdesk/data/time-gate/49a1da2b473b4916824db37a0c3c10fc/`
- 每轮 `public-result.json` 保存公开返回；`boundary-*/` 保存三份核查与来源凭证；`news-*/` 保存发现请求、全文审核及放行/拒绝回执。

这次迁移提供独立预测前置筛选接口。现有原油新闻相关性及十类归档流程继续使用原来的接口。返回字段的日粒度与 `published_at` 兼容值说明见 [使用说明](../time_gate_agent/README.md)。

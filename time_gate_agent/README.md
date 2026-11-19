# time gate agent

从 Pro60002 的 `/root/autodl-tmp/wh/moonsword` 迁移的独立新闻筛选 agent，采用该项目 2026-10-27 的 `choice_verified` 版本。来源文件和原始 SHA-256 见 [migration.json](migration.json)。

本包另提供 `LocalTimeGate`，用于研究台 `env01` 的离线新闻时间筛选：只读已爬取新闻，不执行下面原版流程中的联网发现或首次公布日搜索。GPT6.1 Sol 的期货评估使用这个离线模式，详见 [Benchmark 与 env01](../docs/benchmark-evaluation.md)。

给定一条公开预测问题，先分别执行三份答案首次公布日核查，再抓取候选全文并做独立语义审核。只有日期、原始版本、正文完整性及无答案泄露核查全部通过的原文片段才返回给预测模型。任一公布日未核实，返回 `unavailable` 和空来源；没有日期兜底，也不把空新闻当作答案信号。

## 运行

Python 3.9+，已有并登录的 `codex` CLI；默认使用 `gpt-6.1-sol` / `medium`。无需安装 Python 第三方依赖。PDF 全文提取额外需要 `pdftotext`；缺失时该 PDF 不放行。

在项目根目录执行：

```bash
python3 -m time_gate_agent search \
  --task time_gate_agent/examples/fed-january-2025.json \
  --query 'Federal Reserve monetary policy outlook January 2025'
```

也可审核指定原文；重复 `--url` 可在同一核验边界下检查多篇：

```bash
python3 -m time_gate_agent open \
  --task time_gate_agent/examples/fed-january-2025.json \
  --url https://www.federalreserve.gov/newsevents/pressreleases/monetary20241218a.htm \
  --url https://www.federalreserve.gov/newsevents/pressreleases/monetary20250129a.htm
```

`verify --task ...` 仅核验边界。`--model`、`--effort`、`--timeout`、`--output-dir` 可配置模型及本地审计目录。每次运行创建新目录、执行新审核；不会把此前问题的审核缓存当作本次结果。

任务 JSON 必须含 `case_id`、`question`、`cutoff`、`target_date`，可带公开 `options`、`answer_type`、`resolution_rule` 等字段。`cutoff` 是原任务的时间；核实成功后，沿用原版规则，取结算日与三份核验得到的最早实际公布日中更早的一天，整天排除。日期按原始日历日期判断，不要求新闻提供时分秒或时区。

任务不能传入标签、既有预测、评分或未知字段。示例是已发生的历史问题，便于验证首次公布日和拦截结果新闻。对尚无法核实实际首次公布日的未来问题，这个版本会按原规则关闭新闻。

返回的 `published_date` 是核实的日粒度日期。`published_at` 沿用原协议，以该日 `23:59:59+00:00` 生成兼容值，不能当作媒体真实首发时刻或直接写入现有新闻库的真实时间字段。

标准输出只含 agent 名称、安全时间策略和通过的新闻；私有审计位置在标准错误输出。原文全文、日期证据、模型记录和拒绝原因保存在默认的 `newsdesk/data/time-gate/<run>/`，目录权限 `0700`、文件 `0600`；该目录被现有 Git 忽略规则覆盖。预测模型只应接收 `policy` 和 `response`。

本地模型调用复用 CLI 现有登录，使用独立临时工作目录和只读模式，关闭 shell、插件、应用及多 agent 工具。日期审核和新闻发现允许公开网页搜索，文章审核只读宿主提供的全文。Linux 原项目的完整实验沙箱和预测评分器没有作为运行依赖迁入。

## Python 接口

```python
from time_gate_agent import TimeGateAgent
from time_gate_agent.model import CodexJSONClient

client = CodexJSONClient()
agent = TimeGateAgent(task, private_dir, client, client, client)
response = agent.search('public background reporting')
policy = agent.policy
```

可注入自己的 `boundary_reviewer`、`discover`、`reviewer`，回调签名为 `(material, schema, private_work) -> dict`；回调必须独立于预测器，保留自己的真实工具轨迹到 `private_work/tool-trace.json`。`FetchedPage` 与自定义 `fetcher` 主要用于测试或已有宿主抓取服务。

## MCP

启动绑定本机的服务，token 文件必须不存在：

```bash
python3 -m time_gate_agent serve \
  --task time_gate_agent/examples/fed-january-2025.json \
  --token-file /tmp/time-gate-agent.token --port 17891
```

随后让 MCP 客户端启动 stdio 桥接进程：

```bash
python3 -m time_gate_agent mcp \
  --endpoint http://127.0.0.1:17891 \
  --token-file /tmp/time-gate-agent.token
```

服务名称为 `time gate agent`，提供 `news_search`、`news_open`。token 以 `0600` 创建，不输出到日志；HTTP 服务仅绑定 `127.0.0.1`。服务使用时按需核验首次公布日。

## 验证

```bash
python3 -m unittest discover -s time_gate_agent/tests -v
python3 -m unittest discover -s newsdesk/tests -v
```

迁移验收及实际模型/网页测试见 [测试报告](../docs/time-gate-agent-migration.md)。

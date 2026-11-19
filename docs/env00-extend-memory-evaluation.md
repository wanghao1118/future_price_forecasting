# env00-extend 与 Exp00-extend

`env00-extend` 用于记忆泄漏 Benchmark 的闭卷作答。`Exp00-extend` 冻结当前 2026 年 1—10 月的 100 道题，对现有 17 个已配置模型各执行一轮，共 1,700 道模型作答。它使用独立实验目录，不与 WTI、Brent、SC 的行情回测数据或排名混合。

## 两个 Agent

**time gate agent** 在宿主侧执行闭卷权限检查。待测模型每题开启独立请求，只接收同一条中性闭卷指令和题干字符串；不接收参考答案、别名、事件标题、原文、新闻链接、其他题目或历史对话。请求不提供任何工具，禁用新闻、技术指标、文件与网络检索；若响应仍包含工具调用，宿主拒绝该次结果，不执行工具。逐题保存实际模型输入、响应与权限审计回执。

**evaluator agent** 使用现有登录的 Codex CLI，在独立临时目录和独立会话中判分。待测模型回答先落盘锁定，再将题干、参考答案、可接受别名与该回答交给 evaluator。所有已获得的回答都经过实际 CLI 判分，包括与参考答案完全相同的回答；不使用字符串匹配冒充 evaluator。判分可以将多道独立题目批量提交，但须逐题返回唯一题号、布尔正确性与简要理由。

evaluator 关闭联网、shell、应用、插件、外部记忆、浏览器、电脑操作及多 Agent 工具，只依据给定材料判断。数字、单位、日期和合理的等价表达可判正确；不知道、拒答、无关回答、错误量级或多个互斥猜测不能因包含正确字符串而判对。模型答案始终作为待判数据，不能成为 evaluator 的新指令。CLI 参数、结构化请求、原始公开输出、判分结果与审计记录均保留供复查，不导出隐藏内部推理。

实现依据是本机现有 CLI 的可用参数及 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。实际执行仍需检查保存的工具事件和每次请求，不能只靠提示词宣称禁用搜索。

## 准确率和进度

统一采用二元计分：正确记 1，其余记 0。总准确率始终为正确题数除以 100，每月准确率始终为当月正确题数除以 10，运行中不改变分母。完成进度单独展示；未完成的题暂显示“待完成”。

已经完成的题只展示“正确”或“错误”：错误答案、不知道、拒答、无法提供正确答案的调用结果均记 0。执行异常的具体原因保留在审计记录中，主表不另分错误类别。只有 evaluator 返回合法的正确判决，且模型输入和输出的审计通过，才能加 1 分。已得到的模型回答不可被恢复操作替换；若仅判分失败，重试 evaluator 并继续使用同一份回答。已经判分成功的题在续跑时直接跳过，不重跑错误答案。

## 页面

- `Environment → env00-extend`：闭卷权限、两个 Agent 和准确率口径。
- `Leaderboard → Overview`：记忆泄漏总准确率图，以及模型 × 1—10 月准确率和总准确率表。
- 每个模型页面的“记忆泄漏实验记录”：模型名（实验）、每月准确率、总准确率及进度。
- 点击记忆泄漏实验记录：查看各题题干、参考答案、模型回答、正确性、evaluator 理由和可下载审计记录。
- `Leaderboard → Experiment Setup`：显示独立的 `Exp00-extend` 配置与进度。

现有价格实验与记忆实验使用不同的目录、状态和前端选择，互不替换。模型页面和 Overview 通过已有定时读取更新实验进度。

## 文件和接口

数据位于 `newsdesk/data/memory-evaluations/`：冻结题库、只含题干的模型输入、模型回答、evaluator 记录、逐题结果、运行汇总与冻结执行代码分别保留。模型连接复用现有 AIHub 私有配置；密钥不写入实验文件、日志或页面。

```text
GET /api/research
  memory_benchmark: {experiments, runs, models}
GET /api/evaluations?benchmark=memory-leakage
GET /api/evaluations/<memory-run-id>
GET /api/evaluations/<memory-run-id>/cases/<case-id>/trace
GET /api/evaluations/<memory-run-id>/cases/<case-id>/trace?download=1
```

这些只读接口兼容现有公开代理白名单。实验执行通过本地 CLI 完成，不向公开入口增加启动模型调用的权限。`scoring_policy` 标记 `binary-correct-over-all-questions-v1`，`graded` 保留实际 evaluator 判分数，`resolved/completed` 单独表示完成进度。

```bash
python3 -m newsdesk.memory_evaluation prepare --experiment Exp00-extend
python3 -m newsdesk.memory_evaluation run --experiment Exp00-extend --workers 8 --per-model 2 --judge-workers 3
```

恢复时复用既有实验、冻结题库和冻结代码，不重新创建实验、不更换题目。完整闭卷正确率仍只诊断历史事实记忆，不能单独证明训练数据污染。

## 本轮结果与验收

2026-11-03，`Exp00-extend` 已完成：17 个模型各 100 题，1,700 道作答全部通过实际 Codex CLI 判分与独立文件审计，未留未完成题。所有模型使用同一冻结题库 `memory-leakage-2026-v2-b789cb70c771`，每题独立、只输入题干，模型端工具和检索均为零。总准确率固定除以 100，分月固定除以 10。

| 模型 | 总正确率 |
| --- | ---: |
| gpt-6-sol | 28.00% |
| claude-fable-5-1 | 23.00% |
| gpt-6.1-terra | 22.00% |
| gpt-6-astra | 17.00% |
| gpt-6.1-sol | 17.00% |
| deepseek-v4.1-flash | 14.00% |
| claude-haiku-5-5 | 13.00% |
| kimi-k3.1-pro | 12.00% |
| gpt-6-luna | 11.00% |
| doubao-seed-2-0-lite-260428 | 7.00% |
| deepseek-v4-pro | 5.00% |
| qwen3.7-plus | 4.00% |
| kimi-k3 | 3.00% |
| hy4-preview | 2.00% |
| glm-5.3 | 1.00% |
| glm-5.3-flash | 1.00% |
| hy3 | 1.00% |

完整分月数值及逐题结果在页面与 `newsdesk/data/memory-evaluations/experiments/Exp00-extend/final-summary.json`，独立审计为同目录 `audit.json`（verified 1700 / 1700，errors 为空）。模型页“记忆泄漏实验记录”与 Overview 的分月表均已核对。

执行期间记录了两次调用配置修订：Astra 使用已有 Azure Chat 路由、Fable 使用已有 Anthropic Chat 路由；HY4 在保持输出预算和型号不变的情况下设置 reasoning_effort=none。修订前后参数及哈希保留在 `profile-revisions/`；只恢复未取得回答或判分的题，没有重跑已经判错的模型答案。早期 checkpoint 的 333 个已提交回答或判分文件最终哈希全部保持一致。

验证包括：13 项 runner 测试、10 项模型与权限门控测试、3 项记忆接口测试；前端检查覆盖 17 个模型、187 个分月及总分单元格、100 题可达性、二元显示和原行情实验隔离。真实合成判分测试覆盖金额单位等价、错误数值、多选猜测与 UNKNOWN；另只读人工抽查了跨 14 模型的 20 条正式判分，未见实质误判。

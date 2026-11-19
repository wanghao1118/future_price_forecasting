# Exp05 / Exp06

两批实验都使用回测 v2 `qa-6508a948e223740f22dd`，每个模型 124 题，WTI 41、Brent 42、SC 41，共 17 个作答模型。

| 实验 | 环境 | Skill | 状态文件 |
|---|---|---|---|
| Exp05 | env03 | s01 | `newsdesk/data/evaluations/batches/Exp05.json` |
| Exp06 | env04 | s02 | `newsdesk/data/evaluations/batches/Exp06.json` |

每个实验独立冻结题库、新闻快照、技术快照、Skill 正文和 SHA-256。评分沿用方向正确率、涨跌幅 MAE、最高价 MAE、最低价 MAE；实验完成后再进行批次审计。

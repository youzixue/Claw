# Claw 只读工具契约

## 使用边界

`claw_ashare` MCP 只代理本机 FastAPI 的 GET 接口。它不得训练模型、生成或提交订单、修改参数、创建快照、写笔记或直接访问数据库。写操作只允许用户在 Claw 页面或明确批准的工程流程中触发。

默认 API 根地址：`http://127.0.0.1:8000/api/v1`，可通过 `CLAW_API_BASE_URL` 覆盖。

## MCP 工具

- `ashare_review_history`：列出三阶段不可变复盘快照。参数：`phase`、`review_date`、`limit`。
- `ashare_review_snapshot`：读取一个复盘快照及结构化归因。参数：`snapshot_id`、`include_attributions`。
- `ashare_review_attributions`：按结果交易日、赛道、归因类型或风格读取误差样本。
- `ashare_market_regimes`：读取版本化市场风格历史。
- `ashare_prediction_runs`：读取不可变生产预测运行台账。
- `ashare_training_runs`：读取训练记录、指标和验收结果。
- `ashare_shadow_runs`：读取同一冻结候选集上的 Champion / Challenger 配对分数。
- `ashare_shadow_evaluations`：读取累计时间外指标、风格切片与人工审批资格。
- `ashare_deployments`：读取当前分赛道部署状态及追加式人工晋级/回滚事件；MCP 不提供审批或回滚写工具。
- `ashare_prediction_quality`：读取预测真值与快照质量审计。

如果工具返回 HTTP 错误：

1. 报告后端不可用或接口错误，不伪造数据。
2. 不改用未经治理的网页结果替代本地真值。
3. 给出需要启动/检查本地 Claw 后端的最小操作建议。

## 主要本地 API

- `/daily-review/snapshots`、`/daily-review/snapshots/{id}`
- `/daily-review/attributions`
- `/market-regime/history`
- `/model-lab/runs`、`/model-lab/training-runs`
- `/model-lab/shadow-runs`、`/model-lab/shadow-evaluations`、`/model-lab/deployments`
- `/governance/prediction-quality`

## 输出要求

引用工具结果时始终保留：`review_date`、`analysis_trade_date`、`as_of_at`、`data_version`、`model_version`、`quality_status`。不要只复制候选名称和概率而丢失时点与版本。

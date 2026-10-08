# Claw 只读工具契约

## 使用边界

`claw_ashare` MCP 使用两类审计读口：九条既有 GET 白名单，以及独立 SQLite mode=ro + query_only + autoflush=False + 显式读事务/有界文件读取。模型五条 GET 已移除，禁止 ensure/init 或回退 HTTP。MCP 不训练、下单、改参、建快照、写库、补采、结算或推送；缺表返回 unavailable。

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
- `paper_experiment_report`：十二个独立账户当前协议下的完整周期报告；可选 `account_name`。这是即时汇总，不是历史 as-of 快照；不创建或刷新账户。
- `paper_daily_outcomes`：必填 `target_date`（YYYY-MM-DD），可选 `account_name`；只读取已结算候选结果和对照，不触发结算。
- `paper_candidate_shadow`：必填 `trade_date`，可选 `route` 和 `limit`（1–200，默认 50）；读取有界不可变研究材料。标签不是成交净收益。
- `paper_c3_events`：必填 `trade_date`，可选 `keyword`、`event_type`、`version`、`page`、`page_size`（1–100）；只读事件与通知回执。C3 仅研究，不属于十二个执行账户。

DSH 注册名带前缀 `mcp__claw_ashare__`。全部工具拒绝未知参数和越界分页；新增模拟盘工具严格校验 YYYY-MM-DD 日期。
HTTP 重定向被拒绝，
避免跳转到未审查接口。读不到不能自动升级为写工具。

**不接入** `/paper/challengers/comparison` 等隐含创建/刷新账户的 GET；GET 方法名不能证明只读。
仅暴露已沿业务实现审查的白名单，不提供账户刷新、自动交易开关、买卖、回放、结算或重算。

如果工具返回 HTTP 错误：

1. 报告后端不可用或接口错误，不伪造数据。
2. 不改用未经治理的网页结果替代本地真值。
3. 给出需要启动/检查本地 Claw 后端的最小操作建议。

## 主要本地 API

- `/daily-review/snapshots`、`/daily-review/snapshots/{id}`
- `/daily-review/attributions`
- `/market-regime/history`
- 模型 runs/training/shadow/evaluation/deployment 通过本地 SELECT 读取；原 `/model-lab/*` 五条读取链可能缺表建表，禁止用作自动化读口。
- `/governance/prediction-quality`
- `/paper/experiment/report`、`/paper/audit/daily-outcomes`
- `/paper/research/candidate-shadow`、`/paper/research/c3/events`

## 新增本地证据读口

共22个 MCP 工具。下列8个必填 trade_date，建议显式 as_of（ISO/Shanghai）；拒绝未来截止。
默认分页50，最大200（盘前最大100）；上游预算/缺帧/截断不能省略。

| 工具 | 核心参数/边界 |
| --- | --- |
| ashare_review_readiness | phase；已存日历、K线日期、冻结快照、12户结算、材料；返回 input_fingerprint |
| paper_research_artifact | section=summary/signal_portfolio/post_exit，account_name/code/cursor；读既有按哈希出版材料，不出版 |
| paper_execution_evidence | section=summary/orders/fills/trades/positions/cycles，account_name/code/cursor；全版本事实与当前协议分离 |
| paper_decision_trace | account_name/code/run_id/strategy_version/log_id/cursor；状态变化/复用ID，缺日志不证明未扫描 |
| paper_notification_ledger | account_name/code/cursor；精确信号ID联结回执，sent不证明用户收到 |
| ashare_market_review_universe | section=page（旧分页）/summary（全存储宇宙批统计）/features（紧凑特征页），cohort=all/rising_or_limit/non_rising；cursor股票代码、limit≤200。include_history仅旧page；批处理始终近6日K+有界采样分钟，非历史策略分母 |
| ashare_price_evidence | code，period=daily/sampled_1m/sampled_5m；日K价基、观测目录、归档哈希/时钟/覆盖 |
| ashare_premarket_context | limit；上一冻结复盘、跨休市PIT新闻、外盘最新投影（不认证历史）与纽约参考时钟 |

DSH 配套3个工具（无 MCP 前缀）：claw_review_guard_status、claw_review_report_read、
claw_review_report_save。最后一个只向 outputs/dsh_reviews 原子追加有限研究 JSON，
不写源码/交易库；同任务/日/输入指纹/协议键复用，输入变化另追加。
自动化流程及报告参数见 automation-contract.md。

## 输出要求

引用工具结果时始终保留：`review_date`、`analysis_trade_date`、`as_of_at`、`data_version`、`model_version`、`quality_status`。不要只复制候选名称和概率而丢失时点与版本。

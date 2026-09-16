---
name: ashare-daily-review
description: "Use for A-share premarket, intraday, or postmarket review; next-session limit-up and promotion-pool analysis; market-style diagnosis; prediction miss attribution; or requests to optimize Claw's local A-share algorithms without leaking future data or tuning production from one day."
---

# A股每日复盘

## 核心职责

把 GPT 用作研究编排、证据归纳和代码审查层；把日期、涨跌停、指标计算、训练、回测和版本切换交给确定性代码。始终基于本地冻结数据，不凭记忆补全 A 股事实。

## 执行顺序

1. 明确复盘阶段：`premarket`、`intraday` 或 `postmarket`。阶段不明确时根据用户语义选择，并在输出中声明。
2. 明确 `review_date`、`analysis_trade_date` 和 `as_of_at`。盘前/盘中不得读取当日收盘 K 线；盘后不得覆盖早先快照。
3. 优先读取已冻结的每日复盘快照；可用时调用 `claw_ashare` 只读 MCP 工具。不要让 MCP 训练、改参、生成订单或写数据库。
4. 先检查 `quality.status`、关键数据源和警告。关键源缺失时降级结论，不把缺失候选归因为“未入池”。
5. 分别复盘资金面、消息面、基本面、技术面、市场风格、涨停/高标和预测漏斗。
6. 把结论标记为：`事实`、`统计关联`、`待验证假设`、`模型决策`。自动归因只说明错误落在召回、排序、校准或交易门禁的哪个位置，不宣称市场因果。
7. 给出下一交易日预案时，列出触发条件、失效条件、数据时点和观察池；不得承诺收益或把概率写成确定事件。

## 预测优化规则

- 保持 Champion 冻结。单日复盘只追加样本和假设，不直接修改权重、阈值或特征。
- 先修数据真值和候选召回，再评估排序、概率校准与可交易性；不要用全市场负样本准确率掩盖低基准率失败。
- 只采用按交易日展开的 walk-forward；训练、校准、验证窗口必须时间隔离。
- 至少报告 PR-AUC、Brier、ECE、每日 Top-K 精度/召回和候选池召回。按市场风格分层；聚合指标改善但多数可评估风格恶化时不得晋级。
- 历史 K 线重建面板只能用于预训练和因子筛选。只有冻结的生产预测快照能用于 Champion/Challenger 晋级验收。
- 挑战者通过离线门禁后只能在同一冻结候选集上影子运行；先读取影子累计评估与部署审计，只有 `manual_review_eligible` 且人工输入确认短语后才允许覆盖概率排序，MCP 本身永远只读。
- 每项算法修改只对应一个可证伪假设，并保留基线、数据版本、特征版本、代码版本和回滚点。

## 固定输出结构

按需使用以下结构，缺失部分明确写“数据不足”，不要猜测：

1. **时点与质量**：阶段、日期、as-of、版本、缺失源。
2. **市场状态**：资金、消息、基本面、技术面、风格及证据。
3. **涨停与高标学习**：板块主升/轮动/个股独立/高标投机的可观测特征。
4. **预测漏斗**：候选池召回、主榜召回与精度、概率校准、可执行门禁。
5. **误差归因**：命中、召回漏失、排序漏失、门禁拦截、主榜误报；说明因果边界。
6. **下一步**：继续观察、离线实验或影子验证；不得直接日更生产参数。

## 参考资料

- 处理交易日、涨跌停和真值边界时读取 `references/domain-contract.md`。
- 调用本地 API/MCP 或解释字段时读取 `references/tool-contract.md`。
- 设计训练、验收、影子运行和回滚时读取 `references/evaluation-policy.md`。

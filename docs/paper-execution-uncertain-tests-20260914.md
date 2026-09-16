# 本轮13：模拟卖出未知执行结果上层传播测试

## 所有权与边界
仅新增 `backend/tests/test_paper_execution_uncertain_20260914.py` 和本文档。
未修改业务源码、共享 fixture、生产数据库；没有调用业务 HTTP API、真实订单或网络日历。
生产 marker、事务传播和上层 catch 实现由父任务负责。

复用上一轮 `prepare`、`persistence_fault`、`snapshot`、临时 SQLite
`quote_execution_env` 和禁止 `_sync_from_source` 的日期/日历 fixture。
风险检查和卖出指标 context 的 mock 仅用于隔离异常传播路径，不证明风险或选股策略通过。
明确强制退出测试持仓；行情验收、T+1、service、broker、book、买费分摊、ORM、
SQLite COMMIT、新 session 查询和原幂等键回放均为真实调用。

## 覆盖（10 cases）
- 即时自动卖出最终 COMMIT 前失败：原 ValueError 向上抛出并携带待核对标记；
  无 skip_sell/blocked 日志调用或持久记录；运行 error 日志一次；
  fresh session 经济证据与之前完全一致，订单未伪造 rejected、累计成交为零。
- 真 COMMIT 后丢失 ACK：同样向上传播、不伪造失败日志；
  fresh session 保存完整 PaperTradeLog、PaperSaleAccounting、TradeFill 和 filled TradeOrder；
  比对真实成交数量/价格/费用/时间及订单关联，原幂等键返回原 fill_id，不重复改变经济证据。
- 明确业务拒单：只移除临时库入场买费证据，让真实 book 的买费保护报 HTTP 409；
  service 正常生成 rejected 回报，上层真实生成 skip_sell/blocked 自动日志，
  不记录未知提交 error，不增加成交或改变现金/持仓。
- marker 原对象、原类型、不污染其它异常：ValueError、CancelledError、OperationalError。
- 真实事务边界的 ValueError、CancelledError、OperationalError 与 rollback cleanup 失败：
  原异常对象继续传播并标记，事务 context 和交易锁复位。

未重复父任务 CAS 拒单/并发测试；未追加 scheduler 模拟场景。
scheduler 其它账户退出可继续、失败后禁止新开仓由现有
`test_paper_position_risk_transactions.py` 与父任务最终广回归验证。

## 本轮验证与冻结
命令（backend cwd）：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false CLAW_DISABLE_SCHEDULER=1 /usr/local/bin/python3.11 -B -m pytest tests/test_paper_execution_uncertain_20260914.py -q -ra --tb=short -p no:cacheprovider
```

首次完整运行：3 failed、7 passed。原因是测试 round context 未带 records，
真实 _spot_by_code 按既有规则失败关闭，未到提交故障点。
修复 fixture：将同一临时库 seeded StockSpot 冻结进 records/records_by_code；
未 mock 行情验收、未放松故障到达断言。

修复后完整运行：**10 passed，1 warning，4.14s**，退出码0。
warning 为现有 python_multipart PendingDeprecationWarning。
两个文件至此冻结，不因后续源码更新反复运行；最终广回归交父任务。

本次仅提升订单/成交/持仓经济证据与自动交易日志异常语义的测试覆盖，不调整收益、
风控阈值、交易规则或页面行为。

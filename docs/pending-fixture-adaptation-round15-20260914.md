# Round 15：三个旧 pending 测试的显式输入适配

## 边界

仅修改 `backend/tests/test_pending_t_buyback_validity.py`、`backend/tests/test_continuous_paper_experiment.py`、`backend/tests/test_deferred_buy_confirmation.py`，以及本独立记录。未修改生产源码、公共 conftest、其它测试/helper、父文档；复用已有 `paper_pending_fixture.accepted_frame(db, payload)`，该函数仅在测试临时数据库中建立 QuoteRound 和 full 本地交易日日历，不修改 spot。没有新增风险绕过、mock 新时钟 validator、删除断言或 skip。

## 适配内容

- **T 回补**：文件内 reconcile 增加默认关闭的 `accepted=False`。仅期望 filled/partial 的首轮显式 `accepted=True`，由原 quote 工厂构造快照、建立一致的健康 Tencent 轮次证据，固定 `_public_order_clock` 为测试的撮合时刻。原 decision、confirmed_at、TTL、持仓/卖出身份不改；缺值、过期、回归、历史证据破坏等负面分支不补合法证据。
- **连续实验**：12 账户使用原先固定的 2026-09-08/09 业务日期，提交时显式指定 decision_at/as_of_at/decision_round_id；每个独立撮合帧配置版本与数据库 QuoteRound 一致，真实 ledger 使用固定本地时钟。同日卖出也使用合法撮合帧，以确保仍由真实 ledger 的 T+1 拒绝，而不是被缺时钟意外提前挡住。保留所有止损、成交、持仓、交易日志、净收益与报告断言。
- **E2 排队**：原提交明确独立 queue-decision，正向成交使用 30 秒后的 queue-fill，显式 source/received/updated 时钟和完整使用字段；风控拒绝分支保留原输入。非空 round context 不回退 StockSpot，因此必须放入实际行情快照，不能仅放 code 占位。
- **确认日志矩阵**：只有 filled、partial 系列首轮和 broker_rejected（须真正到达 broker 才能测试拒绝）补合法匹配证据。负面 quote/depth/risk/version/cancel 输入保留。快照包含日志读取所需 name/price；第二轮 waiting 不补新合法时钟。保留历史候选不变、日志观察时刻、成交数、佣金、订单与研究转换断言。

## 调用次数诊断

确认日志矩阵原 quote 校验发生在撮合预检；新 `pending_fill_timing_evidence` 在查询本地日历及 QuoteRound 后重新读取真实 dispatch 时钟并再调用 `_execution_quote_status`。因此进入 dispatch 的分支必须多一次 quote 调用，断言按“原预检次数 + dispatch 分支次数”精确调整；没有去掉统计或 mock validator。risk 与 broker 次数断言不变。

## 限定验证

工作目录 `/Users/youzix/WorkBuddy/Claw/backend`：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false CLAW_DISABLE_SCHEDULER=1 /usr/local/bin/python3.11 -B -m pytest tests/test_pending_t_buyback_validity.py tests/test_continuous_paper_experiment.py tests/test_deferred_buy_confirmation.py -q --tb=short -p no:cacheprovider
```

最终结果：**113 passed, 1 warning in 15.52s**，退出码 0。完整日志 `/tmp/claw-round15-adaptation-final.txt`。warning 为 Starlette 的 python_multipart 导入弃用提示，与本次适配无关。

剩余：本次限定三文件无失败；没有扩展运行父全套或新 clock 测试。本适配只恢复测试输入与当前强制合同一致，不证明全部生产路径或其它测试通过。没有使用网络日历、生产数据库/API、真实下单或部署。

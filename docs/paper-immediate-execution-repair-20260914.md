# 第10轮：内部即时模拟成交统一预撮合验收

日期：2026-09-14。阶段：源码修改与隔离回归；**没有本轮受控部署、下单或实际库写入**。不宣称整个执行链、物理可用时间或策略收益已完成验收。

## 1. 已确认漏洞和源码改动

第8轮公开HTTP入口强制行情验证，但 `SubmitOrderCommand.require_immediate_quote` 默认False，内部调用未传该标志时跳过验证，直接把请求价当成交价。旧即时分支涵盖自动买入、仓位退出、T回补及Challenger内部前向调用；即使正常配置主要使用下一轮撮合，关闭defer或落到即时分支仍不能成为无行情成交授权。

本轮只改两个生产文件：

- `backend/app/trading/service.py`：所有paper即时委托无条件调用既有验收函数，不再读取require_immediate_quote来决定是否验收。该字段保留构造兼容、默认True；显式False/None/0等也不能绕过。新增 `risk.paper_immediate_execution`，保留 `risk.paper_public_execution` 原键兼容已有消费者。
- `backend/app/trading/paper_public_execution.py`：复用同一函数/模块，不另造撮合系统；合同升为 `immediate_paper_fill_v2_20260914`，scope=all_immediate_paper_orders、mandatory=true。

保持原限价、整笔成交或拒绝、原参与率的五档深度；以真实限价内VWAP执行，不按自填限价编造成交。不加自动排队/部分成交回退，不支持把market请求当限价成交。

## 2. 决策轮次和查询后时钟

- 命令已有decision_round_id必须精确匹配实际StockSpot.quote_round_id；非空原轮次不符时直接拒绝，不拿另一轮重贴订单依据。
- 原as_of_at如提供必须匹配该QuoteRound.as_of_at，且轮次as_of必须同交易日；保留原值，不用个股source_quote_at替换轮次汇总as_of。没有as_of时只采用已验收同一轮次的汇总as_of。
- 继续要求已确认本地完整交易日历、连续竞价09:30–11:30/13:00–14:57、健康腾讯QuoteRound、精确提交时钟、个股源/接收/提交三时钟有序且不晚于原决策时点、原90秒新鲜度配置、涨跌停边界和有限有序盘口。
- 服务调用前取实际 `_public_order_clock()`；验收函数完成全部数据库await后再次取实际钟。回拨、跨日、查询后进入午休/收盘竞价或报价过期均拒绝；fillable时冻结 `dispatch_validated_at`，作为本次dispatch逻辑filled_at，不使用上下文committed_at冒充新的验收时刻。
- 该第二次验收之后、到broker调用前没有新增DB await；**它仍不是落账/物理COMMIT钟，见下方明确遗留。**

失败仍保存新TradeOrder rejected和该次拒绝理由，无TradeFill/新PaperTradeLog/持仓改变；风控阻断优先、dry_run仍不匹配/不下单。同委托幂等重试仍返回原结果，不为旧回报重做历史认证。

## 3. 未改变的边界与交易影响

- `defer_until_next_round`的submitted分支及两种后续reconcile、封板queue登记/FIFO原分支保持。已sealed queue只登记不即时成交；queue开关为True但未封板而落到即时路径的委托，同样必须通过本次强制验收。
- 公开入口仍无条件禁止Challenger冒用；内部原策略/账户/来源/信号授权与私有落账作用域不改，未扩大Challenger可交易范围。
- 不改变生产买入阈值、确认/路线/概率门禁、风控规则配置、T+1、旧仓退出版本或费用率。新即时检查可能减少模拟成交、改变即时成交价和后续持仓/收益路径；是口径纠错，不是收益改善承诺。
- 第9轮费用分币合同和新证据表031保留；未补写任何旧订单、交易时间、K线或预测证据。当前旧表历史不能因此被追认为满足v2。

## 4. 隔离回归与旧fixture适配

新增 `backend/tests/test_paper_immediate_boundary_20260914.py`，覆盖默认及显式False/True/None/0/字符串标志的买卖、原限价与实际VWAP、保留决策依据、查询后时间、旧仓版本/新分摊凭证、T+1仍拒绝、dry_run；缺日历/降级轮次/轮次不符/as_of不符/无深度/盘后/过期/源钟缺失/前一日as_of均不能记账。直接查询后时钟测试包括过期、午休、14:57、跨日、回拨、None及有时区时钟。

首次扩大回归暴露4个旧fixture不完整，而非以生产例外放行修复：

1. `test_trading_api.py`内部回撤恢复正例只有裸spot，没有日历/健康轮次/深度，且未固定实际执行钟；明确补这些fixture，保留标准drawdown被阻断、原内部恢复警告断言和20%既有参数。
2. `test_paper_api.py`promotion账户路由正例复用已有qualified_manual_execution fixture，保留实际账户隔离/成交原因断言。
3. 第9轮两个费用测试补当前步独立行情证据，不mock本轮验收器；费用测试原风险mock仅用于账务隔离，不能将其单例当真实全风险链验证。第4步成交与另一委托重复认领的测试采用同一步真实fixture决策时刻，原幂等回放仍保持。

新增 `backend/tests/paper_immediate_fixture.py` 是显式按用例调用的测试帮助函数，绝非autouse或生产fallback；只在pytest临时库造日历/轮次/价量，保留已有独立身份，不豁免风险、不联网。没有修改全局conftest来掩盖失败。

|作业|结果|版本/说明|
|---|---|---|
|bash-200|1954 passed / 4 failed，156.13s|首轮扩大，四个旧fixture缺上述强制输入；含原冻结账本。|
|bash-201|115 passed，19.68s|当时48个新增边界+67公开边界；之后继续加10例，不当最终版本。|
|bash-202|340 passed，46.50s|最终源码的5文件定向：新增58边界、公开边界、费用、trading及paper旧用例适配。|
|**bash-203**|**2025 passed，182.70s，无skip**|最终风险/身份/所有paper/待单/普通及queue撮合/原冻结账本/迁移/部署审计。仅既有python_multipart警告。|

7个本轮Python文件AST检查通过；额外AST确认submit_order不读取require_immediate_quote控制执行，唯一即时验收调用带validation_clock。多组重叠，不累加为独立样本。前端未改，不借此前build冒充后端部署。

最终联合命令（backend目录）：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false CLAW_DISABLE_SCHEDULER=1 \
PAPER_ACCOUNTING_EVIDENCE=/Users/youzix/WorkBuddy/Claw/outputs/postmarket_review_20260914_1717/evidence.sqlite \
/usr/local/bin/python3.11 -B -m pytest \
tests/test_risk*.py tests/test_trading_api.py tests/test_stock_identity_safety_20260914.py \
tests/test_paper*.py tests/test_pending*.py tests/test_continuous_paper_experiment.py \
tests/test_quote_round_execution.py tests/test_deferred*.py \
tests/test_evidence_migration_preflight.py tests/test_deployment_evidence_audit.py \
-q -ra --tb=short -p no:cacheprovider
```

## 5. 明确剩余风险和部署状态

**锁后时钟/提交窗口仍未解决，列为下一优先，不得以本轮通过宣称完整即时可成交。** `service`强制验收后，broker调用私有 `_book_paper_buy/_sell` 还会等待 `_TRADE_LOCK` 和账户/持仓数据库查询。落账时 `_paper_now`消费冻结filled_at，因此这段后续等待可以再次跨越报价有效期或交易时段。下一轮需要绑定已授权执行证据，在锁后/变更前再验，并配套事务失败无半笔落账测试，而不是在本轮未设计的情况下大改5个核心文件。

此外，费用凭证与新卖出ledger已同次提交，但TradeFill仍独立后续提交；物理COMMIT可用钟、同轮重复委托深度容量、跨账户同预算对照、完整K线/共享因子/不可变候选原因链、时间外退出研究及目录释放证据仍在全goal中继续。

2026-09-14 22:48:54只通过ps核实原后端PID67367、20:06:43启动（第4轮受控发布）。本轮未调用生产业务API、未重启、未迁移031；PID相同不证明每个惰性导入模块版本。本轮仅有源码/临时库证据，不能声称第5至10轮已完整部署。最终统一发布仍须停写冷备主库+WAL+SHM、恢复副本演练031/保护触发器/历史摘要及原8000验收，之后还需真实下一交易日前向行情连续性采证。

22:52:27新增sqlite URI mode=ro + query_only + BEGIN一致只读核验：schema仍030，paper_sale_accounting不存在，110模拟交易/2258委托/293回报。模拟交易全部列按id排序compact JSON的SHA256在同一快照内前后均为84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357；这与第9轮tuple-repr摘要算法不同，不直接比较两种hash。产物outputs/repair_validation_20260914_round10/current-deployment-readonly.json，不替代上线行为验证。冻结原9/14 evidence.sqlite完整文件SHA在最终回归中前后仍为68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05。

父bash-200至203均已结束收取。只读审查子代理5afa6bca-fd5c-44ae-84ac-5cc244df6813本轮结束前未收到结论，已请求停止其当前审查；不能计作独立验收通过。上述锁等待风险来自父实际源码检查。

全目标保持active，未标记全部完成。

# 9/14 风控异常与执行汇总边界（第7轮，源码阶段）

## 已确认问题
- RiskEngine.check原先把规则异常追加成WARN决策，却没有追加warnings/block_reasons，最终汇总可能是pass；返回None/错误level/category还可能污染decisions后让序列化再异常。
- 没有启用任何规则也曾汇总pass，不能区分“全部检查通过”与“根本没检查”。
- trading.submit_order规范化局部side但没有同步cmd.side；内部命令BUY/带空格方向会以buy落库，而RiskContext收到原值，规则的action != buy分支可能跳过新买检查。HTTP trading/orders模型原来已有小写pattern，不能把本缺陷夸大成该HTTP模式校验已失效。
- 新委托缺final_level默认pass；普通下一轮撮合与涨停队列也只检查显式block/warn，缺失/无效结论可能未挡住撮合。

## 实际改动
1. app/risk/engine.py
   - 检查RiskDecision对象、枚举level/category、规则名归属及字符串消息/建议，验证后才加入结果。
   - 异常用独立engine类别记录reason_code/error_type。买入及卖出均失败关闭；hold只发警告，不产生执行授权。不将卖出意图当成绕过失效风控的权限。
   - 没有实际执行任何规则时为risk_chain不可用；买/卖block、hold warn。
   - 非明确buy/sell/hold的RiskContext方向为invalid_risk_action，不让规则分支空跑后得到pass。
   - 添加evaluation_status=complete/incomplete和evaluation_errors。checked_rules仅计真正调用的规则，不把合成异常诊断当已执行规则；正常已完成检查保留原pass/warn/block。
2. app/trading/service.py
   - 入口同步cmd.side=规范化后的side，后续风控/实验记录/撮合/落库用同一个方向。
   - 提交、普通deferred复核和涨停queue复核共用_effective_risk_level：缺final_level、无效值、声明incomplete/未知评估状态、pass与block_reasons矛盾均视为block。等待委托记录risk_level也用有效汇总，不把损坏raw pass显示成执行许可。
   - 没有改变原warn策略、涨停FIFO成交量条件、价差/五档参与上限、版本/确认有效期、T+1或费用公式。
   - 正常旧测试/mock的明确pass/warn、未提供evaluation_status仍保持兼容；实际新RiskEngine总返回evaluation_status。本轮不是风险结论完整签名认证或不可配置的证券硬门禁。

## 隔离测试
- bash-179：4文件120 passed / 15.85s（首版）。
- bash-180：新边界+既有风险+交易API 78 passed / 7.26s。
- bash-181：7文件194 passed / 21.19s（含最终方向校验、真实deferred和涨停queue复核）。
- 新test_risk_execution_boundary_20260914.py覆盖异常、坏返回、空/全禁用规则、未知方向、异常不能被正常规则抵消、真实submit方向规范化、无结论不能进broker，以及两条实际待成交复核链在保留候选证据的情况下阻断且零成交。
- 大组bash-182：1829 passed / 9 failed / 1 skipped（166.45s）。9失败均在test_paper_api的账户路由/A-F真实风控正例/暂停演练/高标排队正例：原fixture既没有注册规则，也未提供身份和健康情绪，原先借空风控链pass才得到成交/演练。没有修改生产逻辑去容忍空链。
- 给这4个测试函数（含6账户参数化）单独增加qualified_execution_risk fixture：真实注册原10条规则、隔离健康MarketSentiment、逐场景明确StockTag，结束断言规则确实执行且evaluation_status=complete。保持原账户归属、演练配额、暂停、排队/无持仓断言；不全局污染其它fixture，不mock成风险通过。定向复验bash-183：220 passed / 32.93s；随后原大组最终bash-184：**1838 passed / 1 skipped / 166.36s**，skip为test_paper_accounting_frozen要求显式冻结样本环境变量，不能当成真实历史账本验收。各组重叠不相加；仅既有python_multipart弃用警告，新增边界文件63例。
- 随后显式指定原9/14冻结evidence.sqlite，以mode=ro/query_only独立运行跳过项（bash-185）：**1 passed / 1.50s**。测试核验13账户、110账本/当日25笔、现金/资产/费用残差及前后文件SHA68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05不变。只验证原冻结账本与既有展示口径相容，不是当前实际库/未来费用分配已修复。
- 其余DB/券商均隔离fixture，未连接实际交易接口；不把fixture成交/拦截当策略收益结果。父bash-179至185均已收取，无遗留父命令。

## 已读但尚未修复的独立问题（下一优先级）
- paper.py _effective_board_tag及_risk_check_for_buy仍有旧缺tag按主板默认/只检查特定blacklist reason、tag存在时盖过blacklist的逻辑。
- 公开paper_buy/paper_sell直接持仓/账本操作，不通过submit_order。paper_buy只部分拦截ST/停牌/退市，缺tag或观察板并未可靠拦截；不能声称所有入口现在已统一。
- PaperBrokerAdapter.place_order又回调同一paper_buy/paper_sell；_PAPER_FILL_CONTEXT由adapter自行设置，并不是来源于submit_order的可信授权凭证。直接在现有paper_buy持锁内部调用submit_order可能产生递归/死锁，必须先明确公开路由与私有成交记账边界及兼容契约。
- 正向风控/状态仍是当前可变投影，没有不可变证券目录PIT，也不构成新的策略收益/容量证据。公开路由、probe/Challenger、幂等以及费用/NAV仍须分阶段修复。
- 父已另起只读公开入口审计（3fab6be1-6494-4c45-9665-93c487182f94），不授权改文件/调用业务GET/读写实际DB。旧轮费用和身份只读审查消息未收到，不以其作为验收。

## 部署与边界
- 本轮开始21:17:13及21:34:52核实原8000仍PID67367（20:06:43 round4启动）；第5/6/7轮未进行受控发布。PID不变不是所有懒加载模块版本一致的证明，最终仍须冻结源码、在原服务受控重启并逐接口/任务验证，不能把源码测试冒充已部署。
- 本轮仅两处源模块、一个新增测试及既有paper_api正例fixture/文档，不改生产配置、schema、旧市场/预测/交易证据，无手工订单/调权/晋级。
- 前端本轮无改动，不声称新增UI/构建/交互验证。

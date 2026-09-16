# 第18轮：独立账户内的行情轮次容量约束

实际工作日2026-09-15凌晨，文件名沿用9/14复盘目标。**源码阶段，不是生产已部署、收益改善或交易所撮合证明。**

## 作用域与实现

项目账户用于独立Champion/Challenger反事实对照，而非一个共同资金组合。已有account_name/strategy路由与账本隔离不变，不能按调度顺序让A消耗B的实验盘口。

新增backend/app/trading/paper_depth_capacity.py，并局部修改service.py/public_execution.py：
- 容量键为同一账户名、明确QuoteRound ID、证券、方向；买/ask与卖/bid分开，其它策略账户独立。
- visible_depth_capacity复用原_depth_fill_plan的参与率、腾讯手数及100股取整算法。冻结五档可用上限与原源/接收/提交钟指纹；同轮改变盘口或参与率不能凭新值扩大剩余容量。
- 即时、普通deferred、开板queue三路径在原_paper_order_transaction持有成交锁后，读取已提交回报累计同档消费，再核对**已通过前置风险的原计划**。不在锁内偷偷换档、改价、增量或重做风险假设；计划不足时即时rejected、待单waiting。即使未规划的其它档位尚有量，也不自动重新分配，因此可能少成交，后续重新规划需再次验风险。
- 不写独立预留表/内存缓存。成功TradeFill与原账本/订单在同一既有原子事务提交才消费容量；rollback无占量，提交ACK未知继续沿既有异常链传播。容量不足的诊断也在原锁内提交，不伪造回报。
- account_round_depth合同标识paper_account_round_depth_v1_20260914，scope=independent_account_round_code_side；记录计划量、先前回报数、每档总量/已用量/本次量及指纹。
- 即时回报新增完整immediate_execution_evidence；普通deferred补冻结depth_levels与visible_depth_capacity；开板queue沿用上轮queue_open_depth。容量合同与上述证据一起冻结，原ledger input_sha256关联完整JSON；不是防恶意篡改的签名。
- 旧回报存在但缺本合同/ledger SHA、价量/账户/字段冲突时保守拒绝。读取真实行重新求和，不相信上次capacity诊断中的已用数；缺证据不回填历史。

没有修改auth、broker、纸盘买卖账本、模型/迁移、配置、买入阈值、权重、晋级开关或前端；三个观察测试fixture仅适配显式深度叶值。可能减少重复使用旧盘口导致的模拟成交，并不意味着收益提升。

## 独立审查与修复

取得一次前台限定点只读审查（cell775）：审查者发现首版按TradeFill.fill_round_id/code/side/broker先筛选，再核验合同，会漏掉字段损坏的既有消费。未跑测试/DB/network，不能算部署验收。

父随后在临时库实测：将首笔回报轮次置NULL/别值、证券、方向或broker单字段改坏，第二单仍能重复成交，**5 failed/1 passed**；missing-order对照原先已被拦截。修复为：
- 联合查询本账户/证券/原委托交易日的回报，以及原轮次同证券回报，不只依赖待验证的回报字段筛选；
- 交叉看回报、冻结合同、ledger中的轮次与委托归属；一致的其它历史轮次、其它方向和其它策略账户不串池；
- 被选中的同池回报必须与原身份、原合同、实际价量/逻辑时间及SHA一致；不把字段损坏当“从未消耗”。

本次修后64项定向通过。独立报告是修前审查，修后验证由父执行；不冒称最终源码已获第二次独立全审。上轮未送达的账户作用域后台报告未计入证据，已请求停止，不再扩展。

## 测试与真实失败记录

新增测试：
- test_paper_account_round_capacity_20260914.py：34例，五执行路径的同池两单/有剩余量但非无限量、六种即时/普通/开板交叉、五路径账户独立、真实并发即时买卖、实际book后失败rollback不占量、旧合同/量/账户/回报身份/同轮盘口损坏、新轮恢复、买卖不同侧、禁止锁外调用。
- test_paper_capacity_receipt_identity_20260914.py：独立审查后新增6例，覆盖上述被过滤字段损坏与缺关联委托。
- 新计40例；多数使用真实临时SQLite、真实book/手续费/回报/COMMIT，risk与信号mock仅隔离容量机制，不等于生产风控或策略可行性证明。

过程：
1. 初版新24例 **23 failed/1 passed，9.32s**（bash-261）：18个超容量情景实际多填，5个账户独立对照虽正常成交但缺新证明；rollback对照通过。
2. 新24+上轮queue31+轮次推进24：**79 passed/25.83s**（bash-262）。
3. 首完整联合 **2646 passed/24 failed，351.20s**（bash-263）：失败均为三份观察矩阵fixture的原mock planner返回100股但空档位[]且spot缺买卖档位，不是放松生产容量规则的理由。
4. 审查新6例修前 **5 failed/1 passed，3.87s**（bash-264）；修后新40+逐委托推进24 **64 passed/21.18s**（bash-265）。
5. 仅补原三fixture的价格/手数及原100股对应level/price/quantity；原quote_wait/depth_wait、假broker/观察组件边界及116条assert AST完全不变。三文件 **114 passed/9.98s**（bash-266）。这些仍是原有mock观察矩阵，不能冒称新增真实book测试，也未mock新容量validator。
6. 最终完整联合 **2676 passed/351.40s，无skip**（bash-267），包含本轮40新例及全部原风险、时钟、事务、CAS、费用、Challenger组；仅既有multipart弃用警告。各组有重叠不相加。13份输入源码/测试AST与SHA前后不变，三fixture原116条assert AST不变，日志与/tmp逐字节核验、冻结复盘库SHA通过，见final_manifest.json。

全部测试关闭归档/调度、隔离SQLite，原复盘库只读。所有失败日志保留。证据目录outputs/repair_validation_20260914_round18含源码输入SHA/AST、原assert快照、日志及只读部署审计。

## 运维与限制

02:03:18原8000仍PID67367（9/14 20:06:43），schema030；query_only同快照110交易/2258委托/293回报，当次无submitted/partial paper委托。完整有序全列交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357；原冻结复盘库SHA仍68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05。

最终回归后2026-09-15T02:09:45.182916再以query_only同快照核对，原PID67367/schema030、三表110/2258/293及全交易SHA均与02:03审计相同（after-tests-readonly.json）。

未迁移、停服/重启、手工下单、部署或改写历史。同行情ID内约束不等于跨不同ID同源别名识别，也不证明新轮是真实增量供给；上轮逐委托三钟推进规则仍保持。封板累计量/FIFO近似不在本次五档路径内。TradeFill无数据库append-only约束，本补丁不是全账本对账/防恶意多字段联合篡改；整行回报删除或旧split-commit孤儿的跨委托资源核对仍需补充，不能仅靠本池统计宣称历史完整。

本轮只用当前进程既有成交锁，不提供跨进程容量CAS/物理COMMIT证明；也未把前置风险变为锁后最新完整风险。还需完成锁后最新身份/风险、原decision证据全集、共享因子/K线全消费者和不可变全链原因、同预算时间外容量/退出研究、031受控发布与真实交易日前向连续性验收。总目标保持active。

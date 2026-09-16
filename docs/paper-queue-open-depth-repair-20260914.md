# 第17轮：涨停排队开板后的整笔五档数量验收

实际工作日2026-09-15凌晨，文件名沿用9/14复盘目标。**这是源码验收，不是线上已部署、真实撮合或收益证明。**

## 已确认的缺陷与改动范围

旧reconcile_paper_limit_up_orders开板分支只调用_conservative_execution_price验价，然后按原限价填满整笔。即使卖盘为空、只有100股而委托300股、盘口量非法，仍可能落真实隔离账本；涨停价截顶还可能掩盖超限盘口。封板累计成交量/FIFO近似不是本轮修复对象。

生产代码仅局部修改：
- backend/app/trading/paper_public_execution.py：把即时成交原有盘口校验原样抽成depth_quote_rejection_reason，避免另一套重复规则；新增queue_open_fill_evidence复用同一校验和原_depth_fill_plan。
- backend/app/trading/service.py：仅queue开板支路接入以上验收，新增当前轮次的等待诊断及成功时冻结深度证据；不动普通deferred、sealed触发条件、风控参数和账本/auth/broker实现。

## 新契约

- 校验源盘口的有限值、涨跌停边界、价格顺序、买卖交叉；缺/坏数据不当作合法卖盘。沿用即时成交对缺档/空档的现有规则，不宣称逐档快照完整性或逐笔真实订单验证。
- 腾讯量单位为手；沿用原PAPER_DEPTH_MAX_PARTICIPATION_RATIO，各档先取整100股，再按原限价筛选。必须覆盖整笔数量；不足则原submitted委托继续waiting，不产生部分成交、不通过现价或累计成交量补足开板深度。
- 成交仍按**原涨停委托价**保守记账，不用更低现价或五档VWAP取得虚假价格优势；五档VWAP只作证据。原不利滑点验价仍保留。
- 失败诊断存queue.open_depth_evaluation，带实际轮次、观察时间、原因、可见量和档位；这是可变的最新诊断，不冒称不可变候选事件。
- 成功证据queue_open_depth_v1_20260914放入原pending_execution_timing后一起冻结为JSON；逐笔TradeFill.raw_json存原合同，ledger_execution_timing的input_sha256关联完整冻结合同（包括本次深度）。仍是关联一致性，不是签名或数据库防篡改。
- scope明确one_order_visible_depth_not_shared_capacity。**没有**跨委托容量预留/扣减、真实排队优先级、物理COMMIT时钟或锁后刷新盘口保证；既有真实锁后/最终落账时钟与原子事务规则继续执行。
- 未改账户独立性、生产买入门槛、权重、路线晋级、历史交易/预测/行情；可能减少缺量条件下的模拟成交，不代表收益改善。

## 隔离回归

新增backend/tests/test_paper_queue_open_depth_20260914.py：原29参数例，随后加等待→新轮合格仅成交一次与账本await不能改变冻结证据2例，共31例。

原29例首次29fail中部分因测试在原下单之后改变参与率触发既有策略版本门禁；这是fixture问题，不冒称生产漏洞。已把参与率显式设在原订单创建之前，再在未修源码重跑：
- **29 failed/10.87s**（bash-258）：25个负例实际filled而非waiting，4个合法对照正常filled但缺新深度证明；初始fixture错误日志也保留。
- 修复后原29+即时58+即时账本时钟51：**138 passed/34.36s**（bash-259）。
- 普通deferred一手partial与sealed累计量fixture不变；只给test_paper_orphan_fill_guard.py的queued=True实际成交路径、test_paper_pending_clock_20260914.py的queue-open路径明确提供300股及不交叉盘口，以便原故障注入/时钟边界仍真实进入账本，不能因新预检短路而假通过。未删断言、mock新validator或扩大普通partial深度。
- 最终完整联合 **2636 passed/333.24s，无skip**（bash-260），覆盖新31例以及原风险/交易API/paper/pending/Challenger/事务/CAS/时钟与费用组，仅既有multipart弃用警告。各组重叠不相加。
- 成功/失败日志与原/tmp逐字节核验、8个核心源码/测试输入AST及前后SHA检查、原冻结复盘库SHA均记录于outputs/repair_validation_20260914_round17/final_manifest.json。

所有pytest使用临时SQLite并关闭归档/调度；原复盘库只读。无前端改动，无本轮构建或交互验收结论。

## 运行证据与剩余范围

01:37:41原8000仍PID67367（9/14 20:06:43），schema030；query_only同快照110交易/2258委托/293回报，当次无submitted/partial paper单。完整交易有序全列compactJSON SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357；冻结原复盘库803618816字节、SHA68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05。不得把同PID当作所有懒加载模块已知版本。

本轮未迁移、停服/重启、访问下单API或部署。031及此前未发布修复仍须统一受控发布。总目标保持active。

跨委托容量作用域需基于项目独立策略对照语义，而不能直接令Champion/Challenger互相抢量；具体只读审查报告待接收。其余未完成项仍包括封板FIFO近似、锁后最新身份/完整风险、原decision证据全集、共享因子/K线消费者及不可变全链原因、同预算时间外容量/退出研究、真实新交易日前向连续性证据。

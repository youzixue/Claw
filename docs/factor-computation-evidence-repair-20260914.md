# 第23轮：因子计算追加证据（源码阶段，未部署）

## 缺口与本轮边界

第22轮统一了日线/资金输入，但返回的SHA和诊断没有持久化。FactorValue只有日期、证券、因子值和排名，没有实际输入/缺失原因/首次计算时间/版本；重算不能反推历史。此次在**已有显式“计算并存储”入口**追加本次实际计算记录，不注册新调度、不调用生产计算API、不重写旧FactorValue/日K/资金/预测/交易记录。

单股只计算API继续不写表。雷达、晋级与模拟盘决策尚未读取此新表；不把本轮称为完整三模块共享证据闭环。

## 结构及消费

- 新 `FactorComputationRun` / `factor_computation_run`：一批一行，唯一capture_id，日期、真实请求开始/本地捕获钟、协议、完整payload和SHA。批次完整保留requested/attempted/deferred代码、失败原因和成功项；不把无结果/截断/失败股票从分母删掉。
- 每个成功股票保存实际送入FactorEngine的固定30行帧（含None）、逐日质量和资金原时钟诊断、明确为空的context、全部已注册因子结果/置信度/元数据。是**过滤后的实际计算输入**，不是完整原始HTTP响应，也不补造坏源字段被屏蔽前的数值。
- 调用前冻结帧，调用后检查帧和实现未变；成功项在下一个股票/DB await前序列化。漏结果、帧突变、实现突变不能成为合格计算记录。失败项仍留完整批次分母；无代码批次可留空诊断，不称有效样本。
- 指纹取**进程已加载Python callable字节码**及声明的因子方向/依赖/上下文合同、共用检查函数、Python/numpy/pandas版本；不读当前磁盘源码冒充旧进程加载版本。不保存/执行外部字节码，不是签名构建或完整环境锁，也不覆盖任意闭包、外部全局/原生库ABI变化。
- `read_computation`只读明确capture_id，核验行SHA、列与JSON身份/时钟、协议和分母/数值合同；无“按历史时刻找最新并补证”的接口。
- `replay_computation`不读行情/资金DB，不调用交易/训练；只有当前已加载实现描述完全一致才复算冻结帧。payload先拷贝，再进入await；帧或实现中途变更拒绝。结果不一致显示不匹配，空样本matched=None；匹配也只证明该计算在相同描述下能复算，不证明收益/历史可见性/可成交。
- 原48因子公式、FactorEngine/base、资金/K线校验、单股API、权重、排名、策略阈值和交易执行均未修改。缺逐股新闻/基本面/板块等上下文仍未知。

## 提交与历史保护

- 批量入口强制先追加capture，再沿用FactorStorageService的旧格式insert-missing-only和提交。缺033表、序列化/flush/旧值写入错误不允许无留痕地保存新旧格式值。请求失败回滚未提交事务并抛错，不返回committed。
- helper只flush，返回flushed_not_committed；入口必须实际收到提交成功才返回committed。**真实COMMIT后回执丢失无法靠rollback撤销已落库记录**：测试明确此时抛异常，表可能已有记录，不冒称全回滚。capture_id不是HTTP客户端幂等键；新请求可以追加另一个相同数据观察，旧FactorValue唯一键仍避免覆盖/重复旧值。
- 同一capture_id完全相同材料重试幂等；冲突拒绝。新ID的A→B→A保留三次观察，不按全历史内容去重吞事件。
- 模型ORM update/delete保护、SQLite UPDATE/DELETE/INSERT OR REPLACE三触发器保护。
- 033迁移接032，创建空表/索引/触发器。已有表先核对列集合、非空、主键及capture唯一性；不自动重建不兼容表，不承诺任意类型/索引漂移都可协调。原行不回填、不变更；downgrade拒绝删除证据。
- 部署核验脚本纳入factor_values和factor_computation_run全内容摘要、033表/前置031/032表及新旧触发器；默认目标仍030，不偷偷升版。

## 时间与研究资格

明确记录：
- clock_basis=local_capture_before_commit，physical_commit_at=None；
- point_in_time_verified=false，trading_authority=false，promotion_eligible=false，automatic_weight_update=false；
- scope=bounded_per_stock_read_time_daily_research_not_ranked_cross_section。

captured_at是真实计算时的本地逻辑钟，不是历史因子首次可用或物理COMMIT钟。旧交易日的当下计算仍标当下捕获，不能给旧FactorValue补PIT认证。StockKline/FundFlow仍是读取时可变日期投影；完整决策引用、逐股上下文和实际前向策略验收仍待接入。

## 验证进度及限定

- 首个适配器回归54pass/1fail（15.92s）：上轮100只容量测试用空dict代替所有计算结果，违背新的完整结果分母。改成调用真实原引擎的spy，保留100/101、排序去重、截断与计算数断言；真实产生4800旧格式（多数未知）值，并检查capture提交。这是测试fixture修正，不放松结果合同。
- 新证据初组36pass（12.56s）；扩展捕获/迁移/适配器/部署脚本组104pass（37.59s）。含真实032→033隔离CLI、新表/已有表/重复升级、结构不兼容保留旧行、触发器、旧行不覆盖、A→B→A、源后改仍复算、身份/分母/时钟/非有限/PIT误标拒绝、缺迁移、flush/提交/提交后ACK未知等。
- 所有数据库来自conftest或tmp_path；ASGI无生产lifespan，未下单、联网采集、重启、迁移生产或修改前端。源码验收不替代033部署与前向数据验收。
- 04:20:43及最终04:27:34只读原8000/PID67367/schema030：新factor_computation_run不存在，110交易/2258订单/293回报，factor_values及factor_evaluation_run各0；五表全列SHA与行数全部保持。不能称运行中已经在采新因子证据。
- 首联合1159pass（71.62s）；补两个函数共3项内容摘要/结果不匹配回归后，最终 **1162 passed / 74.78s，0 fail/skip**（bash-297）。本轮新增46项测试，旧55项输入测试继续全部执行；3条既有multipart/pct_change弃用警告保留。组间重叠不相加，不是全项目测试或收益验收。
- 最终36个backend输入AST/前后SHA匹配、5份日志逐字节一致、原冻结复盘库803618816字节及SHA保持。首轮补两个测试函数时edit因old_string非唯一被拒绝，改用唯一定位后成功，已纳入最后联合；不掩盖工具失败。
- 最终联合/源SHA/日志逐字节/冻结复盘库与生产对照归档于 `outputs/repair_validation_20260914_round23/final_manifest.json`。新capture的批量体积/延迟、并发及容量预算仍须在受控发布前做负载验证；本轮没有真实前向市场采证。

# 盘中可靠性与单5万元对照优化（2026-09-21盘后）

## 状态

本次为用户“现在优化”的实际代码交付；工程修复与资金研究分开。**已于23:34:59受控更新原模拟盘后端，23:36:34完成发布验收；最终92文件隔离回归3577通过、1指定历史夹具缺失跳过。** A—F及A2—F2均使用本次更新后的共享执行/通知链路，买点规则与执行版本身份不另行旋转。 不凭测试数量、版本号或盘后健康检查推导下一交易日及时性及收益。

任务起点22:55:57原后端PID11913、127.0.0.1:8000，12户启用；181交易账本、364成交回报、2341订单、88持仓、353正式预测批次受检摘要保留，现金对账和六项不变量为0。账户为12个独立5万元，不是共同5万元。

证据：`outputs/intraday_reliability_20260921/`。原工作树已有大量并行改动，以task_start_sources.tgz和task_start_capacity_cli.tgz为基线，不回滚、不提交他人改动。测试使用临时库、禁调度/真实推送、拒绝INET网络，发布前须再核验源码/测试/研究CLI哈希。

## 一、确定性工程优化

### 1. 消费者取旧轮次竞态

原事件消费者在等待dispatch锁**之前**clear/read payload。watchdog占锁期间若新轮到达，拿锁后仍处理旧轮，可能多走过期/降级无效路径。现在取得锁后在无await区间内clear/read最新payload及quality。

- 用真实asyncio Lock/Event屏障复现旧/新quality的四种组合，原代码均先选old，修复后先选new。
- 新轮降级继续失败关闭；处理中新到的下一轮仍唤醒消费者。
- A2 inbox保留全部顺序帧，不拿交易合并轮次替代A2连续证据。
- 不改原风险→主账户/E2→影子/五connected次账户顺序，不调宽TTL或misfire grace。
- **这不能证明历史某次漏买或3.940秒missed就由该竞态造成。**

### 2. 五connected次账户批量确认已处理身份

原消费者对当天全部confirmed逐条查询订单、成交、执行日志，终结事件每轮重复做N+1查询。新增一次调用内的批量**正向已处理集合**，精确沿用原账户+run_id/signal_token判据，分片200，投影必要列。

- 保留全部事件排名和旧日志；不重新排序、不缓存未处理结论。
- 集合内只跳过原规则已经处理的事件；其余仍走原实时_already_processed，处理同一轮内新提交及竞争写入。
- 同轮等待不重复，下个轮次技术等待仍可复验；错误券商/账户/卖单/普通rejected不能冒充已处理。
- 在下一事件ORM对象可能被前次rollback失效时，先用已冻结candidate.event_key查集合，不先访问失效ORM字段。
- 隔离401个终结事件：**1203次SELECT→9次**，结果相同；本机一次测量0.5400秒→0.0079秒。仅此子步骤的合成性能证据，不是全链路盘中延迟。

### 3. 实际处理钟，而非把行情钟称为耗时

- scheduler记录源/接收/采集钟、锁等待、进程内发布→取得锁、各阶段await区间及总派发耗时。
- 最新一次通过原`/health`的pipeline.quote_consumer读取；逐轮INFO日志保留计时。scope明确current_process_only，重启不意味着旧告警消失。
- 七primary候选、五connected事件增加execution_timing；新增log_observed_at而不改旧created_at业务钟。
- confirmed→实际消费只在有对应不可变事件且时钟一致时计算；primary没有同口径影子confirmed就保持unknown。交易日/未来/负时延异常不补0。
- 原字段committed_at是采集完成钟，不冒充DB提交回执；所有commit_known_at仍为null。
- 现有信号研究报告附加真实墙钟观测，旧业务钟字段保持兼容；不写回旧日志。

### 4. 飞书入口补偿、合批与过期原因

- 失败入口记录精确bp-run_id/event_key及原行情/观察钟。检测到原session有未flush的业务写入时先暂存通知，避免Session.begin_nested无条件preflush把通知失败扩散成交易事务失败。该行为与[SQLAlchemy官方事务文档](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)一致，no_autoflush不能阻止这次preflush。
- 原交易所有者完成commit/rollback并关闭session后，独立session重查账户/StockTag，在原TTL内写**同一既有outbox表**及入口审计。
- 调度七primary（包括T买回/E2）、五connected、手动auto/run的事务结束路径均接hook；通知不替交易提交、不改变成交结果。
- hook以一个已有通知poll interval（当前5秒）作为协作取消预算，不是驱动清理也一定结束的硬时限；无待补偿时零DB I/O。通知恢复仍在派发路径内，故障时会增加等待。取消/持续故障仍可能无法补偿。
- 新补偿行有ingress_recovery marker；研究关联仅对验证过的marker及精确账户/版本/日期/股票/来源/run/同轮次允许“交易日志先于通知入队ID”，旧日志关联不放宽。原研究标签未在失败入口冻结，补偿行将其标为unknown，不把稍后重算的市场状态冒称原时点证据。
- 合批不再让放不下的头卡阻塞后续小卡；单卡最多6条，按真实UTF-8字节预算，同股/账户/版本去重。
- 小批且小时额度已用至少一半时，最多借原一个poll间隔等合并；临近源TTL不等。30批/小时、冷却、180秒TTL不调宽。
- 限频/过期保留hourly_batch_limit、stock_cooldown等原因；过期失败入口只审计，不重新包装为当前买点。
- 不发真实测试消息，不追补历史12次入口错误或7条expired。

**持久性限制：session.info仅短暂移交，不是落盘队列。独立commit成功后才可称持久恢复；DB持续不可写、超时后session被丢弃、进程崩溃仍可能失去入口。飞书HTTP成功后本地sent落库前崩溃仍可能重复，非exactly-once；无用户收到/已读回执。不能声称全信号保证送达。**

## 二、单个5万元对照入口（仅研究，不改变账户资金）

复用`capacity_research.py`原first_come/fixed_times/reserve_late三组，在各账户原始合格预留请求之上施加一个显式共享预算，保留原整手、费用、非容量门禁及scope身份。

- 复用原`backend/scripts/paper_capacity_research.py`，支持`paper_shared_capacity_frozen_input_v1`，不新增交易接口/账户。
- 同股跨策略只预留一次，共享持仓/日名额/可用现金均受约束；不按不同策略未校准分数排序。
- 明确保留selected、displaced、independent_excluded及所有状态分母。
- 不补回被共享预算拒绝后空出的路线名额，不缩量凑单、不循环利用卖出款、不模拟反事实组合风控/盘口成交。不是可执行组合回测，winner/净收益保持null。
- 真实9/21全53条信号、12账户保留到capital_readiness.json与capital_all_53_signals.csv：10成交、3撤单、1不足一手、37容量类、2风险阻断。
- 原快照没有统一组合期初持仓/挂单及逐时反事实风控，**不能把12户并行成交叠成单5万元收益**，也不能因此自动给策略加权/淘汰。
- 未提高仓位、未合并账户、未更改策略信号/参数/权重、未晋升C3。
- CLI实际烟测（所有SQLite连接和INET禁用）通过：4条合成输入，每组1入选、1替代、2原门禁失败，30009元预留+19991元剩余=50000元。产物`synthetic_shared_output.json`；这是合成边界验证，不是9/21真实单5万元资金回测。

## 三、验证与发布

最终`release_final_v3`：92个测试文件、3577通过、1指定9/14冻结账务夹具缺失跳过、0错误/失败/重复用例；348.38秒，33次INET尝试被阻断，源码/测试/研究CLI哈希测试前后相同。号段护栏使用23:28:31只读冻结的5592完整代码；发布器固定其数量、代码SHA与SQLite文件SHA，不放行运行库连接。分项证据在`outputs/all_accounts_latest_20260921/`的consumer_*、shared_capacity_*、scheduler_latency_*、push_*、owner_hooks_*、recovered_attribution_*。

保留未通过阶段：原竞态红测试、缺批量/时钟接口红测试；共享预算首次测试把3万元佣金误写5元，实际原公式9元，修正测试预期而非费用规则；一次研究测试文件名不存在导致零测试，改用真实文件重跑。子任务并行哈希变动的运行不作最终发布依据。第一次整组回归因只读审查发现手动hook的close错误可能覆盖原业务错误/取消，主动终止（bash-158），不算通过；新增7个边界测试复现5红后修正，保留原错误/取消，并在取消时不启动可选恢复，重新完整回归。`release_final_v2`为3576通过、1失败、1指定跳过：失败是旧号段护栏测试直接只读连接运行库，被本次更严格的运行库隔离守门拒绝；不是未知号段或交易判据失败。保留该红结果，不关闭守门、不跳过用例，改该测试可接收显式完整代码快照（缺失/空快照失败），先以mode=ro/query_only导出全部代码并锁定SHA，再重跑全部92文件。

### 发布实测

- 原launchd监督服务`gui/501/com.claw.dev.backend`一次平稳SIGTERM，PID11913→15535，仍为`127.0.0.1:8000`单监听；没有另起后端、改配置或迁移。
- 就绪检查和至少60秒后的稳定检查：原13项账务/生命周期检查，以及6项新增功能/12户版本检查均通过。全部12户启用、执行身份与已发布最新版一致；181交易账本、364成交、2341订单核心字段、88持仓、353正式预测批次受检摘要不变，现金与挂单不变、六项不变量为0。
- `/health`已暴露quote_consumer新契约；盘后为not_run，不能称真实盘中消费验收通过。推送worker运行、last_error=null、46 sent/7 expired与基线一致；新增ingress统计为空，未人工制造线上补偿或真实飞书消息。
- **没有全绿**：23:35:43仍记录intraday_fast missed，finish_lateness_sec=1.859，operational_health=degraded。保留告警、不调宽misfire grace；不能把比前次数字小当作改善统计，也不能将盘后非交易任务回调误报成新的漏单。
- 最终证据以`deployment_result.json`、`after_stable_feature_checks.json`、`after_stable_versions.json`、`after_stable_push.json`与`release_final_v3.*`为准。发布守门比对测试时磁盘源码与新进程正常启动；现有系统没有运行时逐模块导入哈希端点，不能声称做了该种证明。
- 实际修改7个app文件、1个既有研究CLI、6个新增测试文件及1个既有测试快照入口；生产范围以`scope_final_v2.json`对任务起点tar归档的差异为准，不把整个脏工作树差异算成本次改动。未改前端，无前端构建需求。

## 四、仍须盘中／多日验收

1. 下一交易时段按精确身份关联确认、实际首次消费、委托、成交/终止、入队与渠道回执；检查时延长尾及过期原因，不只看均值。
2. scheduler missed的全部根因没有闭环；同步CPU、数据库竞争、风险串行处理、行情/影子积压仍可能贡献延迟，不以本次两处修复概括全部故障。
3. 补偿故障路径可能占用一个poll间隔；记录真实影响。持续数据库故障需更强持久入口方案，不能宣称当前volatile handoff可抗崩溃。
4. 单5万元研究还需真实冻结统一状态、跨日费用/T+1结果及组合风控回放；没有验证净收益、回撤或最优仓位。

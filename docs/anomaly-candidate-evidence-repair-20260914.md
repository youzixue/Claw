# 雷达候选不可变原因留痕（第21轮，源码阶段）

## 已确认缺口
- 原 `AnomalyCandidateRecord` 保存 first_seen_at，但每次扫描更新 last_score、reject_reasons_json、snapshot_json；把最后一次拒绝原因或形态配到首次出现时间，会产生错误的事后归因。原表只是最新兼容投影，不是历史快照。
- 曾经推送成功后，兼容状态始终保持pushed；当前轮没有发送，不等于当前又推送成功。原helper按身份合并多条结果时成功优先，也会丢失失败/缺回报及观察提醒与技术信号的区别。
- 本轮通过源码及隔离连续A→B→A/发送→拒绝序列确认并覆盖此缺口，没有重写9/14原复盘或历史数据。

## 实现及影响边界
- 在现有 `backend/app/models/signal.py` 增加 AnomalyCandidateEvidence，沿用候选 record_id 和稳定信号 identity；`backend/app/signal/candidate_evidence.py` 负责严格JSON冻结、摘要与追加。不创建平行选股引擎，不参与SignalPerformance额度、模拟账户、订单、概率校准或自动晋级。
- 每次真实评估生成capture_id，同一批次/候选唯一；相同原始capture重试幂等，内容冲突拒绝。正常重复观测（包括A→B→A）是新的捕获事件，不以全历史内容去重吞掉状态恢复。
- API只局部修改 `_persist_anomaly_candidate_records` 与自动refresh调用链；在首个数据库await之前冻结原候选和门禁输入，之后变更缓存不能改掉原捕获字节。
- 每条证据保存：
  - 原候选/买点字段、原阻断原因、pushable输入、snapshot版本；
  - 原调用链的baseline、策略入选、技术消息构建、观察消息构建、历史绩效过滤、单轮预算和持久化小时预算通过与否；
  - 各条message原身份/证券、signal或observation类型、sent/throttled/disabled、渠道布尔结果及缺失回报；
  - “本轮reported_sent / unknown / throttled / failed / not_dispatched / not_evaluated”与此前兼容表pushed分别保存。已有SignalPerformance只作单独引用，明确可能包含刚刚写入的本轮发送，不冒称另一张新回执。
- 门禁轨迹是“既有阶段列表成员关系”，不是新计算阈值，也不猜测某个未通过过滤器内部的精确数值原因；B类观察不走技术信号绩效过滤的豁免明确记录。
- 即使一条发送成功，其他失败/缺回报仍保留；reported_sent仅表示渠道返回值，不是外部平台幂等收据、用户已读或执行许可。
- 兼容表继续服务现有页面，不用新的审计表替代原推送额度或交易链。新增返回字段candidate_evidence_capture报告记录状态、候选数和序列化字节；返回副本，不修改原缓存对象。未修改前端，不能声称页面已有完整历史事件浏览功能。

## 时间、异常与保留
- captured_at用本机实际本地日期时间，禁止把今天时分秒拼到历史trade_date。非当日刷新明确not_recorded，既不新造历史事件，也不更新其兼容候选行。旧测试通过明确冻结测试机器时间适配，而不是解除此约束。
- capture是调用时刻、发生在数据库提交前，且自动路径目前在发送阶段之后记录：不是行情/因子首次可用、原策略评估开始时刻或物理COMMIT。记录 `historical_pit_verified=false`、`physical_commit_at=null`、`trading_authority=false`，不允许当成历史PIT买入授权。
- NaN/不可序列化快照或错误gate_trace返回unavailable，不把非法值替成中性数；特别覆盖“快照已冻结、随后门禁序列化失败”的误报recorded边界。无法取得结果时unknown，不编造成功/失败。
- 新表写入放在独立savepoint，缺迁移/审计失败不撤销已有兼容投影，也不谎报审计成功；外层提交失败回滚投影和证据并返回unavailable。这是附加审计机制，不为通知提供事务型outbox保证。
- ORM update/delete、SQLite直接update/delete、INSERT OR REPLACE均受保护；正式032迁移追加空表/索引/三条触发器，保留历史，downgrade拒绝删证据。对已存在兼容结构的表补齐触发器，不回填旧快照。
- 每次评估追加会增加磁盘与写入压力。03:36:51只读现库已有33,691条兼容记录，snapshot合计157,635,837字节、最大9,040字节（全历史，非单日新增量）；不能用这些数字推断新表一天精确增长。提供每批payload_bytes后，仍须受控发布前测量真实扫描频次、写入延迟、空间预算与归档方案；本轮不自动删除任何证据。

## 验证
- 首组新22例及原推送回归126pass/3fail：1个旧生命周期fixture使用8/31却未冻结实际机器日期；2个旧返回值全等断言未包含新增诊断字段。适配后139pass/8.84s。
- 原两个大文件共1064条assert节点及相对顺序保持：生命周期732条不变；推送332条不变，另加4条新metadata/缓存不污染断言。旧结果值的比较仍精确，非修改生产门禁迎合fixture。
- 增加真实refresh的6候选逐级漏斗用例、外层commit失败、非法gate_trace/缺回报、正式031→032 Alembic文件数据库迁移（新表与已有兼容表两种，再次upgrade幂等），总新用例33。
- 首完整雷达/资金/预案/买点通知/候选/迁移联合 **1281 passed / 88.06s**。之后补充非法gate_trace与正式迁移路径得到1286pass/1fail：非法列表已标unavailable，但构造诊断仍误调用list.get；统一清空非法gate映射，3个聚焦边界通过，不放松字段验证。该失败与原候选输入哈希保留，修正版另存final-candidate-inputs.json；最终完整相关联合 **1287 passed / 89.92s，无fail/skip**（bash-288），包括正式031→032隔离迁移。所有操作均在隔离fixture中；发送、行情网络与交易调用不接入生产。
- 35个backend候选输入在最终回归前冻结SHA/AST；10个本轮backend输出（2个生产模块、模型、迁移、2个部署校验脚本、2个新测试、2个旧fixture局部）。部署审计将两个候选表加入完整内容摘要，032必须同时具备031和032表及保留触发器；默认校验目标030不暗中推进。
- 有界后台只读审查未收到报告，已请求停止，不将其计为独立审查通过。

## 部署与仍待完成
- 03:36:51及最终03:45:04只读原8000仍PID67367/schema030，新表不存在；110交易/2258委托/293回报、33691候选、2373绩效记录共五表全列内容摘要与行数保持。交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357。35份最终输入SHA/AST及6份阶段日志原字节均复核，原803618816字节复盘库SHA保持；未调用生产业务API、迁移、重启、推送或下单。
- 不宣称全链路已闭合：当前捕获从自动refresh的候选评估末尾开始；纯B1状态消息、其他手动推送入口、发送后进程崩溃的缺回执、完整pre-dispatch持久意图、候选到paper/实际order的不可变关联及前端历史浏览仍未完成。
- 不把旧9/14 first_seen与最后snapshot重新拼成历史证据；原decision/因子availability全集、全模块共享因子、同预算时间外延续/修复/午后与退出对照、031/032和第5轮以后源码受控发布、真实交易日前向采证仍在总目标内。测试通过不等于部署或收益通过。

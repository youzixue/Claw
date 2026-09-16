# 早盘启动隔离研究调整（2026-09-15）

## 范围与结论

盘后保留证据研究：完整交易日截止2026-09-14；不读取生产数据库、不联网、不运行服务、不改总goal。
只新增一个可证伪假设：**对09:35前已进入≥3%强势区的样本，独立冻结的启动门禁若有连续同帧可供给证据，可否形成区别于首次回踩A2的观察事件？**

本次是版本化变体/严格数据契约与连续确认算法，**不是全市场扫描器、全策略回测、实盘入口或收益改进**。没有伪造历史launch谓词，也没有宣称五股通过新算法。强开延续和断板反包未另造引擎；后者结构和PIT不足，留待独立前瞻设计。

文件仅：
- backend/app/paper/intraday_route_research.py
- backend/tests/test_paper_launch_route_research.py
- docs/launch-route-research-adjustment-20260915.md

未改Momentum engine、strategy_iteration_shadow、challenger、生产参数、调度、API、ORM、前端。没有CLI或部署操作。

## 为什么不改生产默认

证据入口：outputs/strategy_miss_research_20260915_1008 下 research_overview.md、stock_study.md、replay_a2_summary.json、latest_structure_results.json。

- A2 momentum_retest_v3在09:35开始，09:35前已进入≥3%区域不允许冒充首次候选。最新保留回放五股30股日0 confirmed，是当前保留证据结果，不是全输入下绝无机会；默认不变。
- 众泰9/11开盘1.96、昨收1.93，开幅1.5544%超过C2 1.5%；不改成1.6%，不做代码特判。
- 国芳已有真实C2 confirmed但无对应买成证明，补一个信号不能解决容量/盘口/存活时间问题。
- 现intraday框架是冻结候选、身份、谓词和事件审计，不能从日K/末态拼造分钟时序。按此框架做最小扩展。

## 接口与契约

纯函数入口 build_early_launch_report(candidates, samples, *, as_of)。
early_launch_variant_contract()返回独立拥有的版本说明；唯一注册版本 research:early_launch_v1。现build_intraday_route_report入口不会自动启用它。

复用原 _candidate_error / _prepare / _event / _markouts / _costs；原A2门禁结果保留，研究谓词与setup_valid/original_trigger分离。账户和生产版本只是同资金基线身份，不创建新账户、不授予该账户新权限。

固定研究观察规则：
- 09:30 ≤ source≤received≤observed <09:35，同交易日、Asia/Shanghai无时区本地钟；显式as_of，未来观察剔除。
- 至少3个不同源帧、持续≥60秒、相邻和首帧距冻结时点≤75秒；源龄≤75秒，无时钟抖动豁免。
- 现价≥昨收的103%且≥冻结trigger_price；这是设计门槛，不是五股拟合。
- 必须逐帧独立launch_setup_valid布尔值、launch_version、launch_policy_ref与冻结predicate_policy_ref对应，以及launch_predicate_ref。不把A2的原拒绝改成通过，不依据价格擅自构造全套选股/资金谓词。
- 必须同source的book_source_at、book_evidence_ref，正数有限买卖一价格/数量、limit_up及limit_evidence_ref。买一≤卖一，现价及卖一必须严格低于涨停才能积累确认。数量仅检验正数，不推导容量或主力资金；不使用日高低判定可成交。
- 缺盘口/缺谓词/身份冲突/未知活动/断帧为unknown；明确活动失效为control且不复活；涨停帧或明确谓词不通过清空持续段。无确认前缀不冒称全天没有机会。
- 同源冲突/乱序沿用原失败关闭；新增研究盘口和谓词叶进入源帧指纹，不能靠更换sample_id绕过冲突。

候选仍须满足原冻结账户/持仓/交易日历/价格基准合同，route_id为A2基线。另需launch_variant：
version、frozen_at、cohort_frozen_at、eligibility_observed_at、evidence_ref、cohort_ref、eligibility_ref、predicate_policy_ref、code、trade_date、price_basis、historical_eligibility_verified=true、sample_role（design/failure_control/prospective）。
三个冻结/资格钟不得晚于候选冻结；证券/日期/价格基准必须相同。历史资格必须由调用者保留的当时StockTag/风控资格证据提供；当前标签不能冒充历史。引用是调用者拥有的证据声明，**本纯函数不认证外部档案真实性**。冻结之后才创建的本研究定义不得回填旧日。

## 五股和失败例：仅设计，不是假回测

所有下面案例均来自既有保留资料，未新造minute/PIT/填0：
|样本|设计作用|本次可支持/不能支持|
|---|---|---|
|000993 闽东电力9/9首板10:54:39；9/10二板09:33:51|普通时段启动与早板延续对比|9/9不属于早盘变体；池首封不是系统可用/可成交钟|
|002912 中新赛克9/10首封09:34:21|早盘缺口最直接设计例|9/11、14四价一字不能当普通可买；缺历史研究谓词/PIT，结果unknown|
|000980 众泰9/8首封09:33:48|早盘设计例|9/10放量失败必须保留；9/11反包非本变体；C2开盘门不调|
|601086 国芳8/28首封09:38:47；9/14 14:44:23|窗口外/高位午后对照|不为了覆盖它扩展早盘窗，C2确认不等于成交|
|000978 桂林9/7首封09:52:33；9/14 09:30:09、开8次|窗口外和极早反包供给风险|9/7留存仅午后；9/14不得凭最终开板次数还原窗口|

保留失败对照（stock_study.md§7/stock_failure_controls.json），每个涨停日选次日收跌、合格主板及正式价格链、代码升序前2：
9/7智慧农业000816/汇绿生态001267；9/8深物业A000011/华锦股份000059；9/9深粮控股000019/中水渔业000798；9/10京基智农000048/华金资本000532。
这是有意挑选失败案例，不是匹配因果对照，不能估计胜率。五股内部众泰9/10、桂林9/11及中新旧板失败也不可删除。
以上全部归为设计样本/失败诊断，绝不标时间外；真实新变体历史结果仍unknown。测试中的价格/盘口是假设夹具，只验证契约。

## 资金与验收边界

继承同一frozen_account_policy、frozen_position、真实回报隔离审计及条件费用框架，未新造收益计算。保留原1/3/5/10交易分钟采样markout；缺端点、覆盖不足、右删失为unknown，不填0，不取未来。
reference_is_fill=false；actual_fill只来自独立给定回报，不归因于新变体事件。executable_return/net_profit/same_budget_performance/full_market_recall均未认证并保留None。
launch_summary所有给定候选（包括失败、无数据、拒绝）均进入分母；evaluation_split固定unverified_not_out_of_time，即使调用者写prospective也不自行认证时间外。

之后若评估收益，必须另用现有同预算容量/排序/退出框架，等额初始资金、持仓数/仓位上限、100股、T+1、手续费/印花税/滑点、涨跌停/停牌、真实卖盘和退出约束均相同；按未用于设计的新交易日walk-forward。不得将已确认事件直接转换成买单或给本次变体自动晋级。

## 验证

指定解释器运行新旧边界测试（最终结果见任务交付）：
```sh
logs/repair-rehearsal-20260915/final-env-round28/bin/python -m pytest backend/tests/test_paper_launch_route_research.py backend/tests/test_paper_intraday_route_research.py backend/tests/test_momentum_retest_shadow.py -q
```

测试涵盖正路径、无副作用/确定性、3%边界、09:35源钟与接收钟、60秒持续、缺盘口/非有限值/假布尔、涨停不可供给、失效不可复活、初始和中间缺帧、乱序/冲突重复、当前标签/未来cohort、未知版本/政策引用、缺markout、全候选分母和生产A2/C2快照未变。pytest conftest仅临时隔离DB，无生产DB/服务操作。

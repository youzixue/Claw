# 三类买点冻结证据研究合同

更新时间：2026-09-10。模块：`backend/app/paper/intraday_route_research.py`。

## 范围

复用 `build_intraday_route_report`、已有报价时钟/去重/窗口覆盖和 1/3/5/10 **交易分钟** markout；不新增买单入口、不改变 Champion/Challenger 生产门槛，也不从收盘涨停池反推出盘中信号。此功能是**已有引擎冻结事件的路径审计及分层**，不是全策略重放、自动形态预测器或可成交回测。

原 B2/F2/E2 比较继续保留。研究输入新增支持 A2、C2、C3；C3 必须 `evidence_only=true`、`account_id/account_name=null`，不能编造模拟账户或实际成交。其 frozen_position 必须声明 `position_basis=not_applicable_evidence_only`。输入合同升级为 `frozen_route_account_instance_v3`，原已冻结账户输入保持兼容。

## 三类假设

| family | 所属证据路线 | 需要的当时证据 | 不能得出的结论 |
|---|---|---|---|
| repair_first_board | C3 | T-1 同价基 close/MA20/limit_streak=0，已登记上一交易日；当前既有 confirmed 事件及谓词引用 | 不能把昨日低于 MA20 本身当买点，更不能因今日涨停就放宽 MA20 门 |
| first_retest | A2 | 候选前真实 armed 锚点及到候选时的连续覆盖声明；candidate→pullback→confirmed，当前冻结门成立 | 未回踩直接封板是独立不可追溯成交的对照，不自动算漏买 |
| second_ignition | A2/C2 | 同日、同股、同账户实例/路线版本的旧候选明确 invalidated；另起新 candidate_id/new_epoch_ref，重新 rearmed→candidate→confirmed | 普通 recross 不等于二次启动；不能复活旧候选、委托或回写上午确认 |

每个候选可提供 `entry_shape`：

- `schema=entry_shape_hypothesis_v1`、`family`、独立 `policy_version=research:...`。
- `frozen_at` 必须等于候选冻结时刻，`evidence_ref` 必须指向当时证据。
- `cohort_ref/cohort_frozen_at`：候选集合在结果出现前冻结的引用/时刻；本模块不自行认证外部引用真实性。
- 首板：`prior_context` 包含 trade_date、observed_at、evidence_ref、price_basis、close、ma20、limit_streak。close 应与候选 prev_close 一致；不可混用复权与不复权。
- 首次回踩：`armed_anchor` 包含 source_quote_at、observed_at、evidence_ref、coverage_ref、coverage_end_at；最后一个必须等于候选冻结时刻。
- 二次启动：`parent_candidate_id/parent_event_ref/parent_invalidated_at/new_epoch_ref`；父候选及其失效报价必须同时提供，引用、时刻和身份逐项匹配。

报价仍需原合同完整身份、三时钟、价基、setup_valid、candidate_active、predicate_ref。新增可选 `shape_stage/shape_event_ref`；确认必须同时具有原 original_trigger=true、当前 setup_valid=true 及事件证据。不能从日内 high/low 猜 stage。失效后本候选永久终止，缺帧不插值复原。

## 输出与分母

新增每个 comparison 的 `entry_shape` 和报告级 `entry_shape_summary`：

- 按日期、账户实例、路线/生产版本、确认研究版本、形态假设版本、结构层、路径结果、窗口及缺证据原因分层。
- 首板保留昨日 MA20 上/下两组，不只筛出后来涨停者。
- 负报价变化、平盘、缺 K/缺分时、无确认、拒绝候选和失效者均不从输入分母删除。
- no_retest_sealed_control、invalidated、coverage_unknown、未成熟窗口独立呈现；unknown 不是亏损或“确认未发生”。
- quote markout 的正负不是交易胜负；价格未必可成交，排队/T+1/费用/完整交易周期需在实际交易研究中另算。
- `full_market_recall=null`、`production_promotion_allowed=false`、`production_permission=false`。仅有若干 supplied candidates 不能声称全市场召回率。
- 冻结 cohort 或事件证据缺失时标 unknown；**不把 9/1–9/10 历史文件现在补贴标签，伪称当时已有新规则。**

## 使用与自测

直接在隔离研究调用原纯函数，传入 owned JSON mappings、明确 Asia/Shanghai naive as_of 和 RouteResearchPolicy。无需 DB 或调度器，也不调用执行/推送服务。输入完整示例与反例见：

`backend/tests/test_paper_entry_shape_research.py`

测试包含低/高 MA20 及负对照、当日 K/未来时钟/错误价基、首次路径/无回踩封板、旧候选失效不能被下午上涨复活、新 epoch 绑定、缺 parent/缺新确认、乱序/空态和未来样本前缀不改已有结论。测试 fixture 是合成边界情景，不是 9/1–9/10 的实际胜率证据。

当前历史限制：9/1–9/4 分时档案不完整；本次新增三类假设没有历史预冻结全市场 cohort/new epoch 账本。可使用现有真实 A2/C3 事件做覆盖审计，但不能假造三个假设已经完成历史统计验证，亦不据此晋级生产。

# 持仓逐帧前向研究观察（本地新增，未部署）

## 目的与入口

`app/paper/position_observation.py` 复用现有 `PaperShadowEvent`，独立 `route_id=position_frame_observation`、`route_version=research:position_frame_v1`、`event_type=position_frame`。不是 confirmed/control 信号，不进入原 Challenger 下单或结算路线，也不增加普通买卖日志或飞书推送。

`paper._run_auto_sells` 在原卖点确定后、任何执行分支之前，同步冻结位置/账户实例、原策略版本、数量/成本/止损/持有天数、该QuoteRound报价、原退出参数及来源、原卖点原因。函数末尾在全部原卖出处理完成后，追加本次实际执行检查状态。不重跑T+1、风险、盘口或委托检查，不改变其顺序和结果。

轻量扫描即使 `log_holds=False` 也可保存持有帧，但 `available_sell_amount` 未被原逻辑计算时保持null；不能将“未算”写成0或true。原 `_available_sell_amount` 的输出是持仓减当日买入的整手量，不包含所有委托占用和成交条件，因此永不据它自动授予卖出许可。

## 时间、身份及所有权

- 仅 `execute=True`、有真实轮次标识、实际当天且不晚于墙钟的QuoteRound引用才捕获；无轮次手工路径、dryrun、历史重放不补事件。
- `position_and_quote_observed_at` 为实际捕获墙钟；`strategy_evaluated_at` 保存原策略使用的冻结轮次时间参数，不是把物理计算时间倒写到过去。
- `execution_checks_observed_no_later_than`/事件 `observed_at` 为全部原卖出处理完成后的实际观测上界，不给各分支猜一个更早首知时间。
- 建仓成本/数量等先转自有值，成交后清仓或加减仓不能修改此前快照。`observed_position_state_hash` 只表示当前观测内容，不冒充完整的历史交易派生position revision。
- 同账户实例/仓位/轮次/策略/数据与执行状态重复时不覆盖最早事件；观测内容变化生成新key。查询或重入墙钟本身不制造新语义版本。

## 持久化与失败边界

- 一批最多256仓位、每条SQL最多32事件；复用现有唯一event_key，ON CONFLICT DO NOTHING，禁止update/upsert覆盖旧状态。
- 使用Core连接级SAVEPOINT，避免Session.begin_nested无条件flush脏业务对象。SQLite仅存在逻辑事务时先开启真正外层BEGIN，防止释放最外层SAVEPOINT意外提交独立事件；是否提交仍由原调用方决定，原调用方rollback也删除本批事件。
- 追加在原卖出处理之后；可选模块导入、捕获和追加异常均有独立边界，非致命存储失败只回滚它自己的保存点并记录异常类型，不提交/回滚原业务事务，不以审计不可用阻止合法风险退出。底层数据库连接失效仍属于整个原事务不可提交的基础设施故障，不能承诺保存点可修复断连。取消继续传播，不伪造完整采样。
- 未成功走完原扫描、未能存储/提交或进程中止的间隙不能当成“策略没触发”。本包没有额外后台I/O或供应商请求。

## 仍未证明

`commit_known_at`、历史日历首知、公司行动/统一价格基准、成交观测钟、完整委托占用和全局卖出许可仍未知；`replay_ready=false`、`execution_permission=null`、无假想成交价。日高明确可能早于建仓，不是持仓高点或卖后路径。

本包不接入利润保护收益/优胜评价，不改变 `profit_protection_inputs` 的拒绝规则，不回填旧仓或交易。真实自然帧验收和受控部署另有证据，不能用隔离测试通过冒充已上线或获利验证。

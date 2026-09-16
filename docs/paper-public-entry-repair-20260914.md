# 第8轮：公开模拟盘入口与内部落账边界

## 范围与部署状态

- 修复用户已采纳复盘中的工程问题；不是收益改进/自动放宽策略方案。
- 原8000 PID67367仍启动于2026-09-14 20:06:43（21:57:06再核验）。第5–8轮未做新的受控部署，不凭PID证明所有懒加载模块版本。
- 没有调用生产买卖/同步/模拟盘业务GET，没有下单、改配置、调权、晋级、改历史市场/预测/交易证据。没有前端改动/构建，本轮为后端源码验收。
- 源码工作树大量文件本来未跟踪；定点改动，不覆盖其他模块。运行测试使用临时SQLite，禁调度/行情归档。

## 已确认漏洞

1. 原 /paper/buy、/paper/sell 自己记账，不经过 submit_order、TradeOrder、TradeFill 与完整风控。
2. PaperBrokerAdapter 反向调用公开路由；直接在公开路由持有 _TRADE_LOCK 后调用服务会递归/死锁。
3. broker 自己设置 _PAPER_FILL_CONTEXT、Challenger/策略版本上下文；这些是元数据，不是服务已批准的证明。仅检查上下文非空会保留旁路。
4. 原公开手动价格直接当成交价，未证明当前交易日、真实行情三时钟、涨跌停与五档可见深度。通用 /trading/orders 也是公开入口，需要同样门禁。
5. 即使共享身份已正确投影，只禁用可配置的 blacklist/observe_only 规则仍可能绕过身份限制。

## 实施

### 公开委托与私有落账拆分

- /paper/buy、/paper/sell 保留公开函数与成功 status/account/trade 格式，但新委托先走统一交易服务，成功额外返回 order/risk/fills。
- 旧落账函数改为无路由、无Depends默认值的 _book_paper_buy / _book_paper_sell。broker只调用私有函数，不再调用HTTP处理函数。
- 手动请求使用独立 _MANUAL_ORDER_LOCK，不持落账 _TRADE_LOCK 进入服务；新正例以超时断言验证不递归死锁。
- 拒绝委托保存TradeOrder及风险/执行原因，然后公开paper接口返回400或原broker拒绝码（例如跨版本加仓409），而不是200“买入成功”。/trading/orders保留原委托状态响应协议。
- 有signal_id时使用账户/方向/信号范围的稳定manual幂等键，重复请求不重下单；证券/限价/数量冲突409。只有历史成交凭证的精确价量/证券重放只返回原凭证，不补造订单、不改原时钟。新成交按TradeFill.broker_trade_id精确读取账本，缺关联409。
- stop_loss_price从公开请求经SubmitOrderCommand、BrokerOrderRequest传递；延迟委托优先显式stop，保留原metadata stop兼容。建仓板块、首次buy_time、跨策略版本禁止加仓、旧仓退出版本和原T+1/佣金税率不变。

### 内部单次调用域

新 app/trading/paper_authorization.py：
- 只由交易服务内部dispatch打开调用域；提交、下一轮深度、涨停队列三个broker调用点均接入。
- 绑定同一asyncio Task、同一DB对象、同一个BrokerOrderRequest对象与冻结叶字段值。
- broker和账本分别消费issued→broker→ledger阶段；同域不可重复消费，跨Task继承ContextVar也不能使用。
- 私有账本校验证券/方向/账户/价量/信号/reason/stop/板块与已授权请求一致；离开域或异常始终关闭/reset。
- 直接调用adapter或私有落账，即使伪造成交/Challenger上下文，都会在数据库访问前403。
- 这是进程内调用链防误用，不是抵抗能任意修改Python代码/调用私有dispatch的安全沙箱。
- broker缺成交id/价量或价量不匹配时拒绝，不再用请求价量拼成filled回报；旧账本同信号不同证券/价量也409。

### 公开HTTP可成交证据

新 app/trading/paper_public_execution.py，合同 public_paper_fill_v1_20260914：
- 明确限价，整笔可见深度覆盖才模拟成交；不支持市价假成交，不自动排队、不部分成交/暗中遗留余单。
- 同次实际本地决策钟；执行验收另读实际本地时间，不使用外部轮次/成交元数据回拨。拒绝跨日、倒退、决策后才可见的新行情。验收时再查新鲜度/交易时段。
- 只接受本地trade_calendar的True/full，已知假日/周末失败关闭，不调用网络或以工作日推测。仅连续竞价09:30–11:30、13:00–14:57；集合竞价不能用连续盘口撮合。
- 证券代码与行情一致、三时钟齐全有序且<=原决策时点；复用原90秒新鲜度，不容许未来报价。
- 精确QuoteRound存在、tencent来源、quality_status=ok、当日、提交钟与spot一致、清单as_of合法；不从旧健康清单借身份。记录清单config/code版本。
- 涨跌停边界完整、现价/限价/档位不越界，档位正价/有限量、单向有序、不交叉或锁定。
- 五档撮合复用现有_depth_fill_plan和PAPER_DEPTH_MAX_PARTICIPATION_RATIO，不修改参与率/预算；成交用档位加权价，不是请求限价。
- 一字涨停没有卖盘、跌停没有买盘、原限价不足或深度不足均不成交。理由冻结在TradeOrder.risk_json.paper_public_execution。
- /trading/orders的公开paper执行同样强制此检查；客户端不能通过额外字段关闭它。两类HTTP入口均无条件禁止Challenger账户，仿造内部strategy/source/signal或上下文不能授权。

### 身份硬边界

_pre_trade_risk_check沿用第6轮current_projection_not_historical_pit共享身份结果。buy的is_tradeable不为True时，追加stock_identity_boundary并强制block；这不是可关闭规则，不影响其它原规则执行和checked_rules计数。未把当前投影宣称为官方身份或历史PIT，未知/冲突继续禁止新买。没有把ST/观察板块等买入身份限制直接套到风险退出sell；但明确停牌标记或suspended板块状态始终禁止卖出模拟成交，不能靠旧隔夜持仓或看似有盘口绕过。

## 验证记录（阶段组有重叠，不相加）

- bash-186首批5文件：253 passed / 8 failed / 43.00s。七个HTTP旧正例缺真实执行证据，另一个直接调用broker测试不再符合私有调用协议。
- 新边界首跑bash-187：53 passed / 1 failed / 1 teardown error / 6.22s。失败为新测试StockBlacklist fixture漏NOT NULL start_date；修正fixture，没有改变生产表约束。
- bash-188：312 passed / 3 failed / 44.82s。旧fixture对同一spot反复赋相同updated_at，SQLAlchemy onupdate用实际晚间时间替代未变值，触发未来钟拦截。改为每次显式构造下一测试轮次和新时间，不放宽时钟校验。
- bash-189四文件：249 passed / 38.47s。
- bash-190扩大：1901 passed / 2 failed / 1 skipped / 160.24s。两个参数均为旧执行版本HTTP测试只造持仓/spot、没有真实风控和行情清单；改为复用显式manual fixture并提供带observed_at的健康情绪，不改连续实验开启与旧仓退出断言。冻结账本skip需要显式只读样本环境变量。
- 新测试覆盖公开真实风控/TradeOrder→TradeFill→PaperTradeLog、T+1/旧仓版本、stop与板块、无成交拒绝落库、幂等冲突、非法日历/时段/时钟/深度/涨跌停、精确清单、延迟验收不可借未来报价、禁用可配置身份规则仍不能买、Challenger伪装、跨Task/DB/对象/参数/次数授权及残缺回报。
- bash-191旧版本与新入口定向复验：88 passed / 6.17s。
- 最终bash-192：**1906 passed / 166.40s**，仅既有python_multipart警告。命令覆盖tests/test_risk*.py、test_trading_api、test_stock_identity_safety_20260914、test_paper*.py、test_pending*.py、test_continuous_paper_experiment、test_quote_round_execution、test_deferred*.py；环境显式设置PAPER_ACCOUNTING_EVIDENCE为原冻结evidence.sqlite，故无skip，原SHA前后核验通过。其余通用环境PYTHONDONTWRITEBYTECODE=1、QUOTE_ROUND_ARCHIVE_ENABLED=false、CLAW_DISABLE_SCHEDULER=1；Python3.11 -B、pytest -q -ra --tb=short -p no:cacheprovider。
- 本轮新边界测试67例，最终源码/测试/文档哈希归档outputs/repair_validation_20260914_round8/final_manifest.json。上述各组存在重叠，不累加为独立覆盖。
- 最终11个本轮Python源码/测试AST解析通过；AST另断言3个service broker调用均走dispatch、两个私有账本函数无路由装饰/无位置默认参数。旧成交正例仅显式补正常身份、原真实风控规则、健康情绪、本地日历、合法QuoteRound/三时钟/盘口；不全局mock风险pass，不修改原账户隔离/费用/T+1/版本断言。

## 实际只读核验

22:01:43使用backend/claw.db SQLite URI mode=ro + query_only +只读事务：
- 9/14日历实际is_trade_day=1、session_type=full。
- 最新QuoteRound=qr-20260914T150502714193-5e3446b3，committed_at=15:05:02.714193，as_of=15:05:00，source=tencent，quality=degraded，reason=fresh_source_coverage=0.4947<0.9500。
- 这不是新HTTP代码已上线，也不证明收盘后可成交；严格新合同会拒绝该晚间/降级证据。没有为了测试而补清单或改旧quality。

## 尚未解决/不能夸大

1. 内部旧即时成交路径require_immediate_quote默认仍False；自动下一轮/队列保留原各自门禁。需要下一阶段继续统一所有直接内部执行路径，不能把本轮“全部公开HTTP入口”说成“所有来源撮合已统一”。
2. _risk_check_for_buy等预筛和私有账本的旧局部风险防御仍需统一；真正服务前检查已用共享身份硬边界，但不同阶段读取时点可能有变化。
3. 单次可见五档是保守模拟证据，不证明真实成交。跨账户/多订单同轮深度耗用、同预算容量、费用守恒/partial-rebuy、开盘NAV与经济损益仍需完成。
4. 目前filled_at仍是匹配验收时点（早于实际数据库提交），不是物理COMMIT完成时间。账本/TradeFill跨表提交原崩溃窗口仍依靠现有orphan guard抑制重放，不声称已实现原子交易。
5. 不迁移/补写过去无TradeOrder的手动成交。没有signal_id的旧表单请求不能推断用户是否重试，不宣称天然幂等。
6. 所有策略共享因子前向证据、完整K线质量/不可变原因链与时间外容量退出研究、统一受控部署/下一交易日连续观察仍在active目标内。

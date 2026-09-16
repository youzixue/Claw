# 第14轮只读审查：普通 deferred / 涨停 queue 的成交时钟、身份与深度

状态：2026-09-15完成一次源码审查并冻结；文件名沿用9/14修复目标。只新增本文，不改源码、测试、配置或共享文件。先执行pwd（/Users/youzix/WorkBuddy/Claw）、检查脏工作树并read AGENTS.md；目标文档此前不存在。未触碰父任务正在修复的test_strategy_iteration_challenger.py。

**结论：第11轮即时scope时钟保护并未覆盖两条待单路径。第12轮原子事务和第13轮CAS/unknown保护真实存在，但不等于锁后行情/策略身份/风险新鲜度或共享深度验收。** 以下是当前源码可定位的条件性缺口，不是生产已发生错误成交的判定。

## 1. 当前担保范围与源码定位

路径均相对仓库根；行号为本次read所得。

- `backend/app/trading/service.py:700–782`：queue登记与普通deferred登记先于即时验收分支返回；普通deferred必须有decision_round_id（756–762）。即时`public_fill_evidence`仅从786–790调用。
- `service.py:1168–1173,1232–1235,1259–1264,1302–1309,1351–1356`：普通待单冻结本次轮次/observed_at，跳过决策同轮和最近评估同轮；验收当前上下文报价、原限价五档/参与率，再过原风险链。`905–910,996–1025,1029–1063`仅指定自动买入策略需原确认TTL及路线重验，不覆盖手工待单、普通卖出。
- `service.py:1539–1549,1581–1588,1611–1653,1671–1728,1789–1796`：queue检查策略版本、跨日/撤单截止、适用的买入有效期、行情及风险；开板/累计成交覆盖是原有两类模拟触发，非交易所真实FIFO认证。
- `backend/app/api/v1/paper.py:1319–1324,1371–1416`：有轮次上下文只取owned snapshot，不回退较新stock_spot；quote状态比较spot轮次与上下文轮次、已提供的时间年龄及正价格。它不是本地完整交易日日历、连续竞价、严格三钟顺序或QuoteRound数据库健康证明的完整验收器；允许缺部分时钟（有上下文必须有源钟）及最多10秒未来容差。
- `backend/app/trading/paper_authorization.py:117–176`：同Task/DB/请求叶值及issued→broker→ledger单次阶段授权仍然有效。`55–98`先commit预检checkpoint，再取得`paper._TRADE_LOCK`，refresh并比较原订单叶值，持锁至统一commit/rollback；`19–26`检查点不含行情、StockTag、黑名单、策略注册表或共享深度预算。
- `service.py:1397–1486,1822–1893`：两条链的新账本/回报/订单处于上述事务；普通失败保留CAS拒单（1487–1490），queue异常向上传播。原子性保护不是冻结验收之外的新授权。
- `paper_authorization.py:187–195`明确：无immediate_evidence_json即返回None。普通`service.py:1399–1421`与queue`1823–1848`均未传该参数；唯一dispatch scope入口默认空串（519–524）。因此两条待单虽实际调用book两阶段检查，也不会读真实clock。
- `paper.py:10042–10049,10107–10108,10195–10202,10251–10252`：买卖book在锁内准备前、库存首次变更前调用检查。当前待单返回None，继续使用`_paper_now`；`paper.py:71–76`优先取fill/quote context committed_at。`broker.py:84–88,167–173`把req.filled_at放入context，无timing时仍用它生成回报；待单req.filled_at分别来自旧observed_at/now（service 1419/1846）。
- 对照即时：`paper_public_execution.py:34–92,124–139`严格检查决策/三钟/QuoteRound、本地日历及查询后真实clock，冻结原报价期限/原session结束；`paper_authorization.py:203–244`再校验锁后/变更前。这份合同要求fill_round==decision_round（209–210），不能原样拿来授权下一轮撮合。

## 2. 可证实缺口及必要条件

### A. 等待后过期、跨会话仍沿旧钟（优先修复）

必要条件：待单在旧observed_at/now通过原轮次、路线、深度或queue触发与风险；订单未被其它任务改动；之后预检查询、checkpoint、成交锁等待或book准备await使真实时间越过报价TTL、原连续竞价结束、买入确认期限或queue撤单时间；现金/T+1/账本其余门禁仍通过。

源码链直接成立：时间只在循环前确定（1168–1173/1539–1549），行情验收后仍await风险（1351/1789）及锁（authorization 73），book时钟guard因空证据跳过；不会因真实时间越界在这条时钟边界拒绝，且新账本/TradeFill可能仍标为旧轮次提交时刻。即使显式传入真实now，也只采一次，不能封住后续等待。自动买单TTL检查同样使用冻结钟，不能用已有TTL宣称已解决。是否最终成交仍取决于其它账务/风险条件，不声称所有过期单必定成功。

最小反例时间轴：源10:00:00，轮次提交/验收10:00:05；允许年龄90秒；等待到10:01:31后订单checkpoint不变，原报价计划仍可进入book。跨11:30/14:57、次日或queue撤单截止是同类反例。无上下文真实now也未在此两函数建立即时路径那样的连续竞价/本地完整交易日合同；不能假设上游调度时间筛选等价于book最终守卫。

### B. “不同轮次”不等于“决策之后的、实际消费的轮次”

1. 普通deferred显式round_id优先于context（1169），排除决策同轮用该值（1233），但spot验收实际比较context round（paper 1381–1387），两者未相等绑定。必要条件：调用者传入B、上下文仍A且quality=ok，订单决策为A，A快照时效/其余门禁可通过。代码会跳过对决策A的同轮排除，使用A深度却在1418/1446–1449记录B。属于内部参数不一致可触发的缺口，不指控当前scheduler实际传错。
2. 普通只排除decision ID和最近last_evaluated ID，无按QuoteRound提交时间严格排序或已消费轮次全集。必要条件：订单尚有余量，合法不同轮B、C被消费后，再给B（不等决策A、最近为C）；B仍通过所用时钟/TTL/风险。可再次用B计划；请求键还含累计成交量（344–354），不能替代一次快照容量消费。自动买入的时钟回拨守卫会挡部分逆序输入，但传入单调now且B仍在TTL内、或非该类订单，不能据此排除全部条件。
3. queue仅在current_round_id非空时排除决策同轮/最近同轮（1644–1646）；缺轮次的硬阻断只对_requires_pending_buy_validity订单（1640–1642）。其余有效策略版本queue在无context时可落到stock_spot查询并产生空fill_round（1845/1888）；登记queue也没有普通deferred那样的决策ID必填分支。是否存在此类历史/内部订单未查库，不断言生产发生。

### C. 锁后“订单没变”不能证明最新身份/风险没变

`service.py:250–322`的stock_tagger与完整风险检查发生在锁前。book买入确实还查StockTag及st/delisting/suspended三类blacklist（paper 10083–10099），不能称为完全没有锁内防御；但不是再次调用完整stock_tagger身份/板块/人工封禁/近期IPO与原风险链。book卖出（10195–10252）重验库存/T+1/费用，不再检查最新停牌身份。

必要条件：锁前通过后，身份或风险发生变化且订单checkpoint不变；该变化未被book现存局部门禁捕获。例如普通卖出的停牌状态在锁前检查后变化，或买入增加非这三类的人工限制/身份冲突，或风险预算/策略路线发生变化。新查询还可能受当前DB事务快照/ORM缓存影响；不能把“执行了一条查询”自动称为跨进程最新认证。即使补时钟合同，此缺口仍独立存在。

### D. 深度/排队成交量可被多个订单复用

- `_depth_fill_plan`（service 450–496）是按单遍历静态五档、按参与率计算quantity；只减本次局部quantity_left，无同股/轮次/方向/档位共享占用扣减。普通每单重新调用（1302–1308）；锁后也不重算。
- 必要条件：两个不同订单（可同账户，也可跨账户）消费相同快照且各自风险/资金通过，其合计计划量超过该快照允许参与容量。订单串行与同单幂等不阻止第二单复用静态深度。例：允许容量100股，两张各100股订单各得100，合计200。是否真实市场后来补充流动性未知，不能以未知补量证明这两个静态证据足够。
- queue开板分支（1716–1725）只使用`_conservative_execution_price`，然后按整张quantity填单（1762/1830），不调用五档quantity覆盖；该定价函数（paper 1656–1685）只读价格，不读档位量，远离涨停时缺卖一还允许last-price fallback。必要条件：出现开板价格触发但可见卖盘不足/缺失，仍可能整笔模拟成交。这是现有近似假设，不能包装为真实深度证明。
- 封板queue以volume-baseline覆盖queue_ahead*ratio+本单（1702–1728），各订单的baseline/ahead冻结于登记（734–736），没有把本系统早先排队单加入共享队列/扣减已分配成交量。必要条件：多单共享相近baseline/ahead，累计成交够各自阈值但不足全体前后次序需求；同一增量可使多单触发。成交方向、逐笔队列与实际FIFO本来不可由这些轮询字段证明。

## 3. 复用现有scope的最小修复设计（仅建议，未实施）

1. 保留现有Task/DB/请求单次scope和原子事务/CAS；在同一`paper_authorization.py`中为即时、deferred、queue区分服务端冻结合同类型，未知/缺失合同对真实填单失败关闭。不要另造ContextVar授权、客户端授权字段或平行下单入口。
2. 服务在完整预检后再采真实`paper._public_order_clock`，冻结owned JSON：订单/代码/账户/方向/成交价量、原decision ID/at、实际fill ID及QuoteRound三钟/as_of/健康版本、验收时间、原quote expiry/session end，以及适用买单原确认expires与queue原cancel cutoff。不把context committed_at当真实当前时间。将显式round_id=context ID=spot ID绑定，缺失拒绝；以受验收轮次时间证据证明fill轮晚于原决策且严格推进，不按字符串ID排序。原决策不改写，新一轮只作为新的撮合观察证据。
3. 扩展已有两个同步`validate_ledger_clock`检查点处理该合同；deferred/queue允许并要求正确区分decision/fill轮，而非删掉即时的同轮约束。每阶段真实clock同日、不回拨、未越原quote/session/适用信号/queue期限；最终检查至库存首次变动无await。成功统一使用before_mutation时刻写PaperTradeLog/TradeFill及queue的逻辑成交诊断；保留原轮次提交/决策时间，physical_commit_at仍不声称已知。
4. 日历只读本地已确认完整交易会话，未知关闭，不调用网络日历loader。保持原期限边界语义（quote/信号现为>过期，session半开，queue当前>cutoff）；若改变恰好cutoff行为须独立说明，不能悄悄重定义策略。
5. 身份/风险是独立后续层：在原事务/锁内复用原stock_tagger和风险链，确保真正重新取所需行并验证版本/原路线，随后再做最终同步时间检查；不能用scope JSON的旧身份当最新身份。若要求跨进程“直到写入未变”，需版本条件/事务隔离契约，不是Python锁一招解决。较晚行情只能使原计划失效或产生明确新撮合评估，不得偷换原决策证据。
6. 深度占用不塞进clock scope冒充完成：最小单进程方案可在现有事务内按实际轮次/证券/方向/档位，从既有TradeFill证据计算已消费量，重算剩余并与本笔一起commit；所有执行路径都参与才有效。跨进程需要数据库原子容量约束/唯一消费证据；单个内存dict或仅同单last_round不足。queue开板需明确容量策略（不足等待或按受控partial规则），封板累计量需共享排位/消耗模型或保守停止“真实FIFO”式宣称；这属于行为变更，应独立验证，不搭便车改收益假设。

## 4. 建议边界测试（父任务后续，不与当前fixture工作并行改动）

使用隔离内存/临时SQLite、本地已确认日历、真实风险注册链；禁止网络loader、归档写入和生产连接。实际锁争用以asyncio.Event同步，区分preflight、lock acquired及ledger准备阶段，不用sleep碰运气。

- deferred买/卖、queue开板/封板：等待锁及最后账户/身份/T+1/费用await分别跨quote expiry、11:30、14:57、次日、原信号TTL、queue cutoff；回拨/None/时区错误拒绝。期限恰等与+1微秒分别断言，配置放宽不能续旧期限。
- 成功等待：新仓buy_time、PaperTradeLog.trade_time、TradeFill.filled_at等于最终同步clock；原决策/quote committed不被重贴，旧partial/旧仓起点不变；失败不新增成交、费用或库存变化。允许预检checkpoint有既有审计/估值写入，不将其误称整请求零写入。
- 轮次：显式B/contextA/spotA；contextB/spotA；missing ID；decision同轮；B→C→B且now单调；时间不晚于原decision却ID不同；三钟缺失/乱序/未来、健康QuoteRound不匹配。每例断言未借另一轮深度/未新生成回报。
- 锁等待中更新股票停牌、人工限制、策略版本、风险预算但不改订单：验证最新检查的明确范围；另保留已有订单checkpoint变化/CAS拒单竞争/unknown commit ACK测试，防止修clock破坏12/13轮。
- 同轮同股多订单/跨账户累计容量超额；一个partial后另一订单及B→C→B重放；失败rollback不吞容量、成功后丢ACK不释放已消费容量。queue开板卖一缺失/零量/容量不足；封板两单共享baseline与队前量，累计量仅够一单。合计成交不得超过被宣称的共同证据。
- scope：合同类型误用、未知/空合同、篡改ID/价量/期限、跨Task/DB、重复消费；不得直接把immediate同轮合同塞给合法deferred。

## 5. 验证与不能保证事项

本次仅静态源码逐段read/grep和文档对照，**未执行pytest、未运行app导入、未创建tmp测试**。以上反例是满足所列必要条件时的源码控制流推导，未冒称端到端复现或盘中真实发生。未触碰任何DB（包括生产只读连接）、HTTP API、网络日历、下单、迁移、服务重启及历史证据；无需前端构建。

参考已read的`docs/paper-ledger-clock-repair-20260914.md`、`docs/paper-atomic-execution-repair-20260914.md`、`docs/paper-rejection-cas-repair-20260914.md`。生产原8000/PID67367/schema030与未受控发布为任务提供/前轮记录背景，本次没有独立运行核查；不得拿当前源码scope/原子事务/CAS或前轮测试数量冒充该生产进程已装载这些保护。

本文不证明物理COMMIT时刻、跨进程全链exactly-once、最新市场实际可成交量、真实FIFO、所有策略/因子PIT、NAV真实性、收益改善或受控031发布完成。只影响后续修复设计的订单/成交时间口径、潜在成交数量及风险控制；本轮自身不改变交易逻辑或任何交易结果。审查到此冻结，不扩展父任务范围。

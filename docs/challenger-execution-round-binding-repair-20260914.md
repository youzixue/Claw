# 第14轮：Challenger真实执行回归及延迟轮次绑定

目标沿用9/14收盘复盘；实际验证2026-09-15凌晨。修复测试资格不代表调优策略，源码保护不代表生产已装载。

## 1. 第13轮遗留12失败已逐条复验

`test_strategy_iteration_challenger.py` 原31例中12例失败，前轮诊断发现未注册风险链；并非因为新风控太严格应关闭。

本轮新增显式、非autouse的 `backend/tests/challenger_execution_fixture.py`，仅由需要实际成交的测试选择：
- 注册并运行现有10条RiskEngine规则，真实submit_order→行情验收→broker→book→SQLite→TradeFill，未mock生产风控或放宽配置。
- 为测试的实际决策日建立本地完整会话与健康情绪证据，查询已有fixture的价格/盘口数值，构造同一owned报价轮次，冻结同一次真实验收钟。保留已有零卖量/缺字段/坏证据，不替测试修“好行情”。
- 源/接收时钟由该测试明确的updated_at前2/1秒形成；不会把旧updated_at重贴为较新执行时刻。数据库QuoteRound、本地日历和独立股票身份仍由原验证器核对。
- shared challenger_env仅加本地8/9月周历样本和禁止联网loader，不注册全局“默认pass”。周历是测试输入，非生产节假日校准。
- 原有9个函数/12参数例显式选择此fixture；保留原断言及已有“只测试路线排序”的外层风险mock。新增25例则连外层_risk_check_for_buy也不mock。
- 同样给 `test_challenger_continuous_boundaries.py` 中原需实际成交的三个函数补显式fixture，原流动性缺失/失效/同轮不重试/首次可用条件和断言不变。
- 第13轮新rollback调用方测试和买点hook原fixture消费者仍纳入兼容回归。

结果：
- 原31例 **31 passed / 11.35s**（bash-230），此前12失败全部消除。
- 扩展4个strategy_iteration文件、continuous、rollback与买点hooks **185 passed / 51.37s**（bash-231）。
- 新逐路线合同 **25 passed / 9.43s**（bash-232）：A2/B/C/D/F2五条路径真实全风控及原限价VWAP成交/唯一凭证/原键回放；15例缺日历/坏QuoteRound/未来接收钟均不得成交；5例正确行情但错误source→账户映射仍拒绝。原confirmed完整行不变。
- 上述不是形态收益/因子IC/路线晋级证明，未更改任何策略或生产阈值。

## 2. 新确认的实际轮次身份漏洞与局部修复

只读审查原 `service.py:1168–1169`：显式round_id优先决定current_round_id，但后续spot实际从quote context取；当参数B、上下文A都非空且不同，可绕过原决策同轮排除并把A的深度标成B的成交。这是内部参数不一致能触发的源码缺口，不断言生产调度曾经传错。

在已有 `reconcile_paper_deferred_orders` 入口局部新增一致性校验：实际撮合时，两个非空ID不同立即409，禁止重贴依据；发生在任何订单查询/审计或账务写入之前。ID相同或省略显式ID仍按原上下文执行；expire_only不消费行情成交帧，保留维护路径。未改历史订单ID/TradeFill、未重命名轮次或重放旧决策。

新增 `test_paper_deferred_round_binding_20260914.py` 的9例：
- 修复前 **4 failed / 5 passed / 4.05s**（bash-233）：买/卖上下文为原决策轮或另一真实轮、显式另写B，四例均未抛所要求的409；正确ID/省略ID及expire_only对照通过。
- 修复后四例要求查询前409、数据库经济与委托完整行保持原样；其它五例保证原decision/fill ID区分及维护兼容。该文件明确只隔离身份绑定，风控/日历复用原事务fixture，不冒充完整撮合资格测试。

修复后9例连同25合同/原31/连续性/行情与原子性组合 **220 passed / 64.85s**（bash-234）。最终纳入全部strategy_iteration/challenger模块的联合 **2474 passed / 293.60s，无skip**（bash-235），只有既有multipart警告，未排除前轮失败文件。九个源码/测试输入文件AST与同次联合前后SHA核对；原31例对应28函数的181条assert AST完全不变；原冻结复盘数据库SHA亦前后不变。成功失败原日志均归档，不累加重叠组。完整命令范围与逐文件hash见outputs/repair_validation_20260914_round14/final_manifest.json。

## 3. 只读审查的其余发现尚未修复

`docs/deferred-clock-audit-round14-20260914.md` 为子任务冻结的修复前静态审查，不含运行复现或部署认证。本轮只补其中B1显式轮次错配；其它仍待继续：
- 普通deferred/queue未绑定即时scope时钟合同，锁等待/账户与费用查询后可能过期；fill_round不同于原decision不能简单套即时同轮合同。
- B→C→B轮次逆序/重放、部分queue缺ID、queue开板容量不足仍可能按旧模型整笔撮合。
- 锁内仅订单字段不变不足以证明最新股票身份/停牌/人工限制/策略风险未变。
- 多订单/跨账户复用同轮静态五档或封板累计量，未形成共享深度原子消耗；真实FIFO不可由轮询量证明。

这些是满足必要条件时的源码控制流缺口，不是生产已发生的事实；不能用本轮31/185/25通过代替修复。下轮优先分合同修锁后时效/轮次推进，再处理共享容量和queue原假设，不放松生产买入门槛。

## 4. 生产、历史和全目标边界

00:30:25只读原8000仍PID67367、9/14 20:06:43启动，schema030；110模拟交易/2258委托/293回报。完整交易行compact-JSON SHA同快照前后且与第10–13轮相同：
`84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357`。

本轮未迁移、重启、连接业务API下单或改写旧市场/预测/交易证据；新测试交易只发生在临时SQLite。未改前端，不声称本轮前端构建或交互验收。第5–14轮尚未受控发布，031费用证据部署和真实新交易日连续性仍待验收。

全goal保持active：以上待单合同之外，共享因子/K线全消费者、不可变原因链、同预算容量及时间外退出对照、生产前向采证仍未完成。不能以测试通过冒充收益提升。

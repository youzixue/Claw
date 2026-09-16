# 第11轮：即时模拟落账的锁后与变更前时钟

日期2026-09-14。状态：源码/隔离测试阶段；本轮没有生产下单、重启或迁移。这里只修复锁及准备查询等待后的时间校验，不把它称为物理COMMIT、最新盘口或全链原子性认证。

## 1. 已确认问题

第10轮service完成行情/日历/深度验收后，broker仍要等待私有 `_book_paper_buy/_sell` 的 `_TRADE_LOCK`、账户刷新、身份/持仓和卖出费用/T+1查询。原 `_paper_now`使用dispatch冻结时间，等待跨过有效期或连续竞价时段仍可能按旧钟落账。

此次把原已验收报价的时效合同绑定到同一Task/DB/请求作用域，两个同步检查点分别位于：

1. 已取得 `_TRADE_LOCK` 后、创建/刷新账户前；
2. 所有本次成交准备await结束后、第一次修改库存前。买入在持仓查询之后，卖出在T+1和费用计划查询之后；此检查与库存变动之间没有新的await。

检查只消费原始已验收快照的时间依据，**不读取较晚报价替代原决策，不认证当前最新盘口或在等待期间没有身份/风险变化**。

## 2. 最小生产改动（5文件）

- `app/trading/paper_public_execution.py`：在既有即时v2证据增加代码、方向、账户、原新鲜度秒数、以最早源钟计算的quote_expires_at及当次session_end_at；原90秒配置和整笔限价/五档参与率不改。
- `app/trading/service.py`：只在即时dispatch把已验收owned JSON文本传入内部作用域；不是客户端可传的授权字段。成功后把落账时钟诊断附到 `risk.paper_ledger_timing`。deferred/queue原dispatch仍不使用即时专属时钟合同。
- `app/trading/paper_authorization.py`：复用已有调用域，新增不可变JSON文本和一次性两阶段时钟状态。验证原代码/方向/账户/成交价量/轮次、源/接收/提交/决策/dispatch顺序、同日、原有效期/原连续竞价结束时刻以及锁前后单调性。显式损坏JSON/未知合同/矛盾时间失败关闭，不能当作无证据deferred作用域。校验仍绑定当前Task/DB及请求叶值，不是抵御任意恶意Python代码的沙箱。
- `app/api/v1/paper.py`：仅两个私有落账函数各增加两个检查。失败HTTP409，原因带lock_acquired或before_mutation及实际尝试时刻；不按过期时间新增交易。通过时用最终变更前检查时间作为新PaperTradeLog.trade_time；新仓buy_time也对应这个时间，旧仓加仓起点/退出版本不变。
- `app/trading/broker.py`：验证真实账本回报的trade_time与作用域最终检查相等，以该时间生成TradeFill.filled_at。新的raw_json包含 `ledger_execution_timing`；不再把dispatch钟贴成稍后逻辑成交钟。

守卫独立版本 `paper_ledger_timing_v1_20260914`，输出原dispatch钟、锁后钟、变更前钟、原轮次、时效截止和原证据SHA。`physical_commit_at=null` 明确不是缺值补零，也不由逻辑成交时间推断物理可用。输入是service已经构造的owned JSON，不枚举/复制ORM对象作为证据。

旧回报幂等重读和历史TradeOrder/TradeFill/PaperTradeLog不补字段；此前v2记录没有本轮ledger guard时，不能自动追认已通过。没有schema变更，仍需前轮源迁移031才可整体发布。

## 3. 保守边界与影响

- 源钟已达到原最大年龄边界时沿用既有 `age > max_age` 规则，恰好等于阈值可验收；超过一微秒拒绝。session结束时刻半开，11:30或14:57立即拒绝。
- 原证据冻结有效期，等待期间放宽运行配置不能延长该笔已验收有效期。本轮没有修改实际配置值；只在测试中验证这一点。
- 时钟缺失/有时区/回拨/跨日、未先完成锁检查、重复消费最终阶段或跨Task/DB/请求不允许落账。
- 拒绝不新增模拟交易/TradeFill/卖出费用证据，不改变库存数量、开闭仓状态；此前账户刷新可能更新既有可变估值/NAV，不能把测试的“无新成交”外推为所有可变估值字段绝对未触及。
- 风控、T+1、手续费/印花税、原成本公式、策略门槛、Challenger路由和旧仓版本不变。有效等待会让新逻辑成交时刻晚于dispatch；过期等待会减少模拟成交。这是时间口径/风控修正，不承诺收益改善。
- 第二阶段以后数据库flush/commit仍需要await；本轮选择“变更前时刻”作为逻辑落账点，不把它冒充物理提交时刻。跨进程/跨账户深度占用、期间最新盘口及身份变化仍不在本时钟守卫的保证范围。

## 4. 验证及遇到的问题

所有pytest用Python3.11，QUOTE_ROUND_ARCHIVE_ENABLED=false、CLAW_DISABLE_SCHEDULER=1、PYTHONDONTWRITEBYTECODE=1、临时数据库及-p no:cacheprovider。新测试真实调用service→broker→private ledger，风险来自原注册规则；实际锁等待用asyncio.Event同步，不靠sleep碰时序。

|作业|结果|说明|
|---|---|---|
|bash-204|186 passed，31.01s|原即时/公开/费用/行情轮次/trading定向回归。|
|bash-205|20 passed / 24 failed，62.04s|新等待测试fixture错误，未把失败当源码已通过。|
|单例诊断|1 failed，2.53s|继续确认正例被更早行情门禁拒绝，未抵达预期锁阶段。|
|bash-206|50 passed，11.20s|修正fixture后，真实锁、准备查询及绑定时效边界全部通过。|
|bash-207|2061 passed / 14 failed / 14 teardown errors，203.29s|新增禁日历loader断言暴露trade_days_between无条件调用loader；失败均为新测试卖出fixture准备阶段，不计通过。|
|bash-208|158 passed，29.30s|显式测试日历loader，禁止网络sync；新增50时钟、58即时及50费用。|
|**bash-209**|**2075 passed，218.72s，无skip**|最终风险/身份/所有paper/待单/普通与queue撮合/原冻结账本/迁移/部署审计，仅既有python_multipart警告。|

新fixture错误及实际修复：

- 对已有StockSpot重新种同一时间戳时，ORM认为updated_at未变，onupdate=datetime.now自动填了实际盘后钟，造成更早三时钟拒绝，锁事件当然未到达。仅在 `tests/paper_immediate_fixture.py` 使用flag_modified显式发送该测试时间列，未修改生产模型/自动时间行为。
- `_refresh_account`和`_stock_info`在风控阶段也会调用。延迟注入最初过早改变模拟时钟，测试到的是service门禁而非private准备阶段；现在只在实际授权ledger stage注入延迟，门禁不mock。
- 原两次next时钟迭代fixture改成用完后保持同一当前时刻，以适应新增锁后/变更前检查，不放宽“最终时间等于已验证值”的断言。
- 早期失败用例出现一次全局日历同步日志（目标仍为conftest临时库，不是生产库）。后加禁止 `_ensure_loaded` 的断言发现 `trade_days_between` 即使日期已缓存也无条件调用loader，14个新卖出测试在准备阶段失败并触发14次teardown错误。未修改生产日历规则换取通过：最终为这个非日历测试显式注入只读取已种9/11–14布尔日期的本地fixture loader，仍由生产方法选交易日并执行T+1；网络 `_sync_from_source` 禁止且断言未调用。原全局loader多余访问列为后续运维/日历边界，不以fixture掩盖源码差异。

新增50例：买卖实际锁等待在过期、午休、14:57、跨日、回拨、None/时区错误时拒绝；账户刷新和最后准备查询延迟；成功逻辑成交时间一致；15类原证据冲突；重复/跨Task；阈值精确边界/配置不可延长和4类损坏JSON。原费用/T+1/版本/幂等回归保持。测试组有重叠，不累加作为独立样本。最终冻结9/14 evidence.sqlite只读回归也通过，完整文件前后SHA仍为68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05；没有重跑并替换旧复盘证据。父bash-204至209全部已结束并收取。

最终命令（backend目录）：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false CLAW_DISABLE_SCHEDULER=1 \
PAPER_ACCOUNTING_EVIDENCE=/Users/youzix/WorkBuddy/Claw/outputs/postmarket_review_20260914_1717/evidence.sqlite \
/usr/local/bin/python3.11 -B -m pytest \
tests/test_risk*.py tests/test_trading_api.py tests/test_stock_identity_safety_20260914.py \
tests/test_paper*.py tests/test_pending*.py tests/test_continuous_paper_experiment.py \
tests/test_quote_round_execution.py tests/test_deferred*.py \
tests/test_evidence_migration_preflight.py tests/test_deployment_evidence_audit.py \
-q -ra --tb=short -p no:cacheprovider
```

## 5. 后续工作

下一优先为现有ledger→TradeFill独立提交及异常路径可能提交半笔状态的问题，必须单独设计事务归属和故障注入回归，而不是用本时钟检查冒充解决。还需deferred/queue的锁后时钟独立适配、最新身份与风控复验、同盘口容量、共享前向因子/K线全部消费者、不可变候选到订单的因果证据、NAV/PIT及时间外退出研究。

23:09:17只读核验原8000仍PID67367/20:06:43，实际schema030，110模拟交易/2258委托/293回报；全部模拟交易按id排序compact JSON摘要84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357在同一只读快照前后相同，也与第10轮同算法结果一致。产物outputs/repair_validation_20260914_round11/current-deployment-readonly.json。8个本轮Python文件AST通过；两个私有落账函数均有精确两个阶段检查。额外AST验证：进入async成交锁后的首句就是锁后检查，最终检查位于首次库存数量/成本变动之前且中间无await。没有业务API调用，该只读核查不能代替在线行为验收。

当前源码未受控发布；不能依赖同一PID来证明所有惰性导入模块版本。最终仍要在安全停写冷备/恢复副本演练031和历史摘要后，于原8000统一发布，再观察真实下一交易日连续性。本轮没有前端改动，不需要新增前端构建；没有下单、自动调权/晋级或改写历史市场、预测、交易证据。全目标保持active。

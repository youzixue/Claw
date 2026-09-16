# 模拟盘新成交的原子记账修复（第12轮，2026-09-14）

## 原因与范围

此前即时、下一行情轮次、涨停排队三条链路均由 broker 调用私有账本。账本的 `_refresh_account` 内部执行 `commit`，持仓和 PaperTradeLog 已经持久化后，service 才建立 TradeFill、修改委托累计成交数量并另行提交。两者之间发生取消、回执保存失败或 broker 返回异常时，会留下账本已成交、委托仍未成交的孤立证据；旧 generic exception 的拒单 commit 还可能提交未完成的账本变更。

本轮只修复**新成交的数据库一致性及异常分类**，不是重新评估买入阈值或重算旧成交。旧孤立成交识别、拦截、幂等键、费用分摊、T+1、持仓版本、盘口及风控规则保留。

## 源码改动

- `backend/app/trading/paper_authorization.py`：增加同 Task/AsyncSession 绑定的事务域；进入时先 checkpoint 保存预检与委托，再取得原 `_TRADE_LOCK`，直到账本、费用证据、回执和委托的最终 commit 或 rollback 完成才释放。包括 CancelledError 在内的 BaseException 均走 rollback 后传播。
- checkpoint 在等待 Python 锁**之前**，避免一个已 flush 委托的会话持有 SQLite 写锁，反过来阻塞另一个持有 Python 成交锁的任务收尾。它可以保存新 pending 委托及预检账户/NAV，不包含新成交，不应解释为“整个请求没有任何写入”。
- 不使用 SAVEPOINT 嵌套提交；不把 SQLite legacy transaction mode 的 RELEASE 当可靠的外层原子提交。
- 私有账本必须同时处于已验收执行域和同任务事务域；元数据上下文仍不能授权执行。原锁不重复取得。
- `backend/app/api/v1/paper.py`：账户初始化、刷新在成交事务域内只 flush，不内部 commit；域外报告/预检保持原语义。锁内重新读取账户，仓位查询在该域用 populate_existing，避免沿用预检阶段缓存的旧数量。
- service 三路径从 broker dispatch 到 TradeFill/TradeOrder 写入均位于同一域。锁内重新读取委托，并比较已验收身份、价量、状态、累计成交及风险原文；等待期间发生变化则要求重读，不用旧请求覆盖新状态。
- 即时和普通延迟路径仅将 **broker 调用阶段**的业务异常在 rollback 后另行保存 rejected；回执构造/flush/最终 commit 异常向上传播，不误写为业务拒单。排队保留异常向上传播的接口语义。
- 若最终 COMMIT 已成功但确认丢失，rollback 不能撤销已完成的事务，**但四类证据应当一起存在**。调用方只能用原幂等键/原轮次读取核对；不伪造新回执，不把它覆盖为 rejected。
- 普通延迟 broker 不接受/无真实回执时，不再计算“0股 partial”。逐单循环刷新 ORM，避免上一单回滚使后续委托过期后触发 MissingGreenlet。
- `backend/tests/test_paper_orphan_fill_guard.py`：历史缺口测试显式 test-only 提前 commit，复现旧部署留下的原始孤立证据；保留所有拦截与原时间/数量不变的断言，不将新原子行为拿来删除历史防护。

## 验证进度

- 首版 6 文件隔离回归：**258 passed，53.83s**，1 个既有 python_multipart 警告，父任务 bash-210 已收取。该次运行之后还增加锁等待委托状态复核和批量 ORM 刷新，以后续最终回归为准。
- 首次扩展 bash-212：**2092 passed / 39 failed，245.63s**。29例为既有 fixture 回执缺 accepted，6例发现真实调用方在 rollback 后访问过期账户导致日志 MissingGreenlet，1例旧测试直接读取 rollback 后过期订单，3例新事务 fixture 把所有账本时钟冻结为决策钟（掩盖实际填单上下文）。源码补充审计调用方/自动买卖/T路径对失效账户、持仓及旧日志的异步刷新；5个旧测试文件明确返回 accepted=True，数据库异常断言改查 refresh 后真实记录；代理修其本地时钟 fallback，不改时间一致断言。
- 修复后5文件定向 **188 passed / 24.37s**（bash-219）。父事务所有权测试首轮因自己fixture遗漏必填order_type：1 passed/10 failed，8.12s（bash-215）；补明确limit后11 passed/5.49s（bash-216），之后继续补全委托类型/决策出处等11个锁等待冲突边界。
- 联合 bash-220：**2219 passed / 252.32s，无skip**。运行中源码又补全了委托类型/均价/原因/决策时刻/版本等比较字段及11个相应测试，所以该次不能单独充当最终源码验收；已对收敛后的最终源码再次运行完整联合回归（bash-221）：**2230 passed / 264.48s，无fail/error/skip**。最终11个源码/测试文件哈希与运行前一致，含新故障注入133例和事务所有权22例；11文件AST、三路径dispatch与TradeFill同域、私有账本无直接commit结构验证通过。冻结复盘数据库完整SHA前后也由显式只读测试验证。各组测试重叠，不相加充当独立用例总数。新事务代理的最终扩展结果由父联合测试覆盖；未把中断前未收到的完整子审查报告计为独立验收。
- 新增专用测试只使用临时 SQLite/显式行情与日历 fixture；局部风险 mock 用于事务失败注入，不代表策略选择通过。
- 未改前端，无本轮前端构建或 UI 交互验收声明。

## 部署与历史证据限制

23:29:23 只读确认：原 8000 服务 PID67367 仍为 20:06:43 启动；schema 仍 `030_kline_observations`。本轮未停启服务、未执行迁移、未下单。源码第5–12轮尚未经过后续受控部署；同 PID 不足以证明所有 lazy import 的内存版本。

生产库通过 URI mode=ro、query_only、BEGIN 读取：110 条 paper_trade_log、2258 委托、293 回执；账本所有列按 id 排序的 compact JSON SHA256：
`84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357`。
与上轮同算法一致，不改旧账本/原复盘目录。只读证据见 `outputs/repair_validation_20260914_round12/current-deployment-readonly.json`。

## 不在本轮完成声明中的项目

1. 单进程 Python 锁不是跨进程/真实券商分布式事务，也不授权直接调用 SQL/Python 绕过服务。新事务不覆盖旧部署已独立提交的孤立账本，后者仍隔离拦截。
2. 即时原快照时间的锁后/变更前校验沿用第11轮；真实物理 COMMIT 时间、延迟/排队路径锁后过期验收尚不能宣称完整。事务一致性不等于可成交证据的新鲜度。
3. 一个数据库事务对应一次新填单，旧合法 partial 已独立完成提交，不随下一笔失败回滚；预检 NAV/账户显示也不是成交事务的全部历史。
4. 没有解决跨账户盘口深度共同占用、同预算容量/路线/退出的时间外评估；没有自动调权或晋级。
5. schema031 的新费用证据表与本轮源码仍需受控迁移/发布和实际新交易日采证，不能用 pytest 通过冒充部署完成或收益改善。
6. 未将上层自动卖出循环的所有 generic exception 日志升级为“提交结果待核对”，也未证明所有跨调用方并发拒单/重试都具备串行化语义。service 的持久化异常不覆盖 TradeOrder 为 rejected，但上层未知错误解释、业务拒绝回滚后另行写拒单与其它执行者竞争的边界仍需后续独立核验；不能把本轮原子数据库保证推广为完整分布式 exactly-once 或全链因果验收。

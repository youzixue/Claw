# 原子模拟成交故障注入（隔离测试）

所有权仅限本 notes 和 `backend/tests/test_paper_atomic_execution_20260914.py`。未修改生产源码、其他 tests/docs，未访问生产数据库、业务 HTTP API 或真实下单。

## 隔离边界

- 已读取 AGENTS、全局 conftest、orphan guard、ledger clock、public boundary 和 immediate quote fixture。
- conftest 导入 app 前把 DATABASE_URL 指向 mkdtemp 临时库，并检查全局 settings/engine；测试另使用 quote_execution_env 创建 tmp_path SQLite。
- 复用 orphan 的明确日期 2026-09-09 和前一日库存；日历缓存和 _ensure_loaded 显式本地提供，_sync_from_source 抛错且断言未调用。
- 仅此事务测试 monkeypatch 风险结果为 pass、关闭 pending entry validity；不声明生产风险通过，不修改生产风险配置。真实报价验收、broker/book、手续费、ORM/SQLite 和 fresh-session 查询保持真实。
- 不伪造 _PAPER_TRANSACTION，直接观察真实 scope；跨 Task/DB 当前契约为 HTTP 403。
- 现金、持仓成本/数量/时间/关闭状态、全部 ledger/费用证据/回执逐行快照；预检 NAV/浮盈刷新不纳入经济变动断言。

## 矩阵

即时 buy/sell、deferred buy/sell、queue buy：
1. 真实 broker 返回非空 fills 后注入 CancelledError、ValueError、HTTPException，fresh session 无新增经济记录。
2. TradeFill add、显式 flush、真实 SQLite BEFORE INSERT trigger ABORT、最终 commit 前故障，回滚全账及委托累计。
3. deferred 第二次 partial 故障保留第一次所有经济记录和订单累计/状态。
4. 真正 final commit 成功后 ValueError 模拟 ack loss：新 session 完整 ledger+回执（sell 含费用分摊），原幂等 key 返回同 fill，非即时同轮重撮合为空。
5. receipt add/final commit 持锁，真实竞争 Task 在 commit 前后仍不能进入；完成后释放。
6. 继承 Task/异 DB scope 拒绝，已有 execution scope 但无 transaction 的私有 book 403。

flush 注入说明：AsyncSession.commit 内部直接调用同步 session.flush，因此测试在 final commit wrapper 显式 await db.flush() 来覆盖异步 flush 故障；真实数据库层 flush/insert 拒绝另由 SQLite trigger 验证。不是把数据库落账 mock 掉。

## 首轮证据（保留）

命令（backend cwd）：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false CLAW_DISABLE_SCHEDULER=1 /usr/local/bin/python3.11 -B -m pytest tests/test_paper_atomic_execution_20260914.py -q -ra --tb=short -p no:cacheprovider
```

首轮 bash-211：**3 failed, 53 passed, 1 warning in 19.11s**，exit 1。
三例为 `test_successful_commit_lost_ack_retains_complete_fill_and_replay[deferred-buy/deferred-sell/queue-buy]`：

```text
At index 4 diff:
TradeFill.filled_at datetime.datetime(2026, 9, 9, 10, 0, 30)
PaperTradeLog.trade_time datetime.datetime(2026, 9, 9, 10, 0)
```

定位为本测试 fixture 把 _paper_now 无条件冻结为 AT，错误遮蔽生产 _PAPER_FILL_CONTEXT 时钟。修正 fixture 为有 fill/quote context 时调用原始 _paper_now，仅无 context 的墙钟 fallback 固定 AT；未删改/放松时间一致断言。

第二轮 bash-213（同命令）：**56 passed, 1 warning in 24.50s**，exit 0。仅修正本测试时钟fixture，没有放松断言。唯一 warning：Starlette python_multipart PendingDeprecationWarning。

收到父通知首版源码已就绪后，第三轮 bash-214（同命令）：**56 passed, 1 warning in 23.64s**，exit 0；本轮未更改测试。

父同时修改源码，以上为各轮启动时加载版本的精确结果；父仍需最终合并后执行相关历史孤立成交/账本时钟/公共边界回归。本子任务未运行或修改其他测试文件。

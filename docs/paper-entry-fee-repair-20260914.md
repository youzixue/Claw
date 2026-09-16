# 第9轮：后续卖出买费分摊与旧成交重复认领修复

日期：2026-09-14。状态：**源码及隔离回归通过，尚未受控部署/迁移031；不是全目标完成或收益验收。**

## 1. 已确认的问题与影响范围

旧 `_book_paper_sell` 按当前持仓周期的全部买入手续费乘以“本次卖量/历史总买量”分配费用，未扣除此前部分卖出已经分摊的费用。例：买200股付5元，卖100股分摊2.50元，再买100股付5元，最后卖出200股时，应分摊剩余7.50元；旧公式给6.67元，少计0.83元。

这是**已实现损益的费用分摊错误**，不是证明现金被重复收费。现金账仍按每笔买卖各自金额、佣金和税费计算，不使用 realized_pnl 作为额外现金扣款；本次没有再次扣现金，也没有修改费率、最低佣金、印花税、成本价公式、交易阈值或权重。

新集成测试同时发现：使用另一委托幂等键但相同旧 signal_id，broker可能把旧账本幂等回报包装成新的 TradeFill。已增加明确拒绝，不允许新委托重新认领旧成交或重打成交时间。同一委托幂等键正常重试仍返回原委托结果。

## 2. 未来费用合同

新增 `backend/app/paper/entry_fee_allocation.py`，版本 `remaining_entry_fees_v1`：

- 按账户/证券完整成交链、trade_time/id排序回放剩余数量和剩余入场费用。买入加入本笔佣金和税，部分卖出从当时剩余费用分摊，再加仓只增加新费用；已平仓周期不泄漏到新周期。
- Decimal分币、ROUND_HALF_UP；最后卖出吃掉全部剩余费用。例如5元/300股分三次卖100股，为1.67、1.67、1.66元，严格合计5元。
- 账户/代码混入、重复或非法ID、缺失/非有限/负费用、非整股、缺失价格、未来/非法/带时区时钟、卖量超库存、重建库存不等于持仓数量等均返回unknown，不用0补缺、不过滤坏历史来制造完整性。
- 对新证据验证版本、账户证券、成交ID、数量、费用前后守恒及此前输入SHA256。存在但损坏的证据不是“无证据的旧交易”；孤立证据、此前历史被改变会阻断。
- 输出明确 `historical_records_modified=false`、`cash_deduction=false`。`prior_sale_allocations_evidenced=true` 在本周期没有旧卖出时也成立，是空集条件，不等于历史已生成凭证或完成PIT认证。
- 旧卖出没有新凭证时按本合同经济回放，显式报告 `legacy_sell_count_in_open_cycle`，**不认证其原分摊政策，不回写其 realized_pnl**。已有历史差额不是本轮自动清零的对象。

私有落账在持仓变动前加载计划。依据unknown时HTTP409拒绝，不得按零买费伪造成交。这可能阻止缺失买入链的模拟风险退出；应报告并核查原证据，而不是为通过测试/放行退出而臆造费用。真实T+1、身份、行情、深度、风险链仍独立要求通过。

## 3. 只追加新卖出证据

新增 `PaperSaleAccounting` / `paper_sale_accounting`，以 trade_id唯一关联本次新卖出，保存版本、实际记录时间、输入摘要、费用前后、分摊数量和该次账面损益。

- 新卖出账本与其费用凭证在同一次既有ledger提交中保存；响应与TradeFill raw_json带凭证，手动入口成功返回亦补充。
- 只读accounting loader按同账户加载并验证新凭证；旧记录没有凭证则不补写。原纯 `accounting_snapshot` 加权经济回放公式不改，新分币凭证为独立字段，不能把二者不同舍入语义混称完全相同。
- SQLite创建表和Alembic031均安装禁止UPDATE、DELETE、同id或同trade_id替换的三个触发器；最后一项覆盖默认recursive_triggers关闭时的INSERT OR REPLACE。只保护新增凭证表，**不宣称所有旧交易表已不可变**。
- `031_paper_sale_accounting` 仅建新表/索引和触发器，不回填旧账本；重复upgrade可补触发器，downgrade拒绝删除历史。仅SQLite保护经过本次测试，不外推其它数据库。
- 审计脚本将新表加入全字段核心摘要。比较器默认仍精确要求030；目标031须显式 `--expected-revision 031_paper_sale_accounting`，要求新表、共9个追加保护触发器、旧表计数/核心摘要不变、禁业务smoke中新表为空。触发器名检查不能替代发布时核验SQL正文。

局限：recorded_at是创建对象时刻，trade_time为逻辑成交时刻，都不是物理COMMIT可用钟；账本到TradeFill仍是后续独立提交。此次阻止重复认领，不等于跨表全链原子事务或历史PIT已解决。SHA是变更检测，不是防任意恶意代码的安全认证。

## 4. 实际只读核验

产物：`outputs/repair_validation_20260914_round9/current-entry-fee-basis-readonly.json`。

2026-09-14 22:25:35，以sqlite URI mode=ro、query_only=ON、BEGIN一致读事务读取实际库，不调用可能写入的业务API：

- 实际schema仍为030_kline_observations，110条模拟交易。
- 9个未平仓的完整数量/买费链均known，各剩余买费5元、合计45元；本周期均没有此前卖出，不能据此推断其它历史复杂交易都正确。
- 110条交易完整行摘要在同一只读快照内前后均为 `32973133349453a07aa1375e43ea18babbd5a71cbb941c4919290b4062980ac9`。
- 这只是费用依据核查。所有结果execution_authorized=false，不代表今晚可卖、T+1满足、盘口可成交或真实收益改善。

22:36:31再次ps确认原8000服务PID67367，启动时间20:06:43（第4轮发布），未操作重启/迁移/下单。相同PID不证明每个惰性导入模块版本；可靠结论是第5至9轮未执行新的完整受控发布。

## 5. 验证记录（有重叠，不累加）

所有pytest使用Python3.11、PYTHONDONTWRITEBYTECODE=1、CLAW_DISABLE_SCHEDULER=1、QUOTE_ROUND_ARCHIVE_ENABLED=false、-p no:cacheprovider及测试临时库。

|作业|结果|解释|
|---|---|---|
|bash-193|120 passed / 1 failed，7.52s|新测试暴露新委托重复认领旧回报；后续修broker，不以删除拒绝断言掩盖。|
|bash-194|121 passed，7.12s|早期3文件定向；此后仍继续强化，不代表最终版本。|
|bash-195|1940 passed / 2 failed，172.85s|两例旧版本退出fixture只有裸持仓、没有买入费用链，新规则正确拒绝。|
|bash-196|59 passed，10.28s|当时费用、经济口径及隔离030→031迁移/旧迁移测试。|
|bash-197|223 passed / 2 failed，31.30s|同一旧fixture仍未完成有效适配，明确保留失败记录。|
|bash-198|168 passed，27.51s|显式补原买入成交/佣金5元后复验旧仓退出、费用、迁移、行情执行及孤立回报防护。|
|**bash-199**|**1967 passed，166.20s，无skip**|最终联合风险/身份/公开及私有模拟/延迟成交/待单/冻结账本/两代迁移/发布审计。仅既有python_multipart警告。|

新费用文件50例、031迁移文件2例（最终collect-only确认52例）；发布审计另增2例。12个本轮Python文件AST通过；纯费用计划import不加载app.db.session或ORM模型，避免只读审计意外初始化应用数据库。

最终执行命令（backend目录）：

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

冻结9/14 evidence.sqlite原文件SHA `68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05`，测试再次验证13账户/110交易/9持仓、现金资产残差及原文件前后SHA；不是当前运行库，也没有重新生成复盘历史。集成费用用例为费用隔离而mock风险结果/走既有内部即时分支；真实风险/公开盘口另有联合测试，不把该单例冒充完整实盘撮合。

## 6. 发布前和全目标剩余

本輪未改前端，不需要为本輪新增构建；此前构建不能当本次后端上线证据。新消费者发布前必须先在停写恢复副本演练、精确迁移031并核验历史摘要，再按原8000受控流程发布；不得在服务仍写入时只复制主库或使用不保留运行配置的全服务restart。

全目标继续active。下一优先是内部旧即时撮合默认分支统一、账本/回报提交边界及原候选执行因果证据；还需全部K线消费者质量、共享前向因子、同预算/同盘口容量与退出时间外隔离对照、证券目录释放证据、NAV/物理可用时间，以及统一受控部署和下一交易日前向观察。没有降低买入门槛、自动调权/晋级、生产/模拟实际下单或历史证据重写。

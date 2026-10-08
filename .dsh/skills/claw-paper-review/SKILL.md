---
name: claw-paper-review
description: "复盘 Claw 十二个独立模拟盘策略 A–F/A2–F2 的候选、买入、卖出、未成交、风控和完整交易周期；用于每日模拟盘复盘、策略漏斗归因、上涨个股与失败对照研究。不是实盘建议或自动调参授权。"
---

# 十二策略复盘

先遵守项目 AGENTS.md；读取相关源码和 .workbuddy/memory/MEMORY.md，不沿用过期共享组合口径。

## 账户边界

A–F 对应 default、promotion、mainline、auction、tenbagger、reversal。
A2–F2 对应 challenger_a、challenger_b、challenger_c、challenger_d、challenger_e、challenger_f2。
以 backend/app/paper/experiment.py 和 account_policy.py 的当前注册及账户参数为准。
十二账户独立核算；不得视为共享资金池或合并成一个可执行组合。C3 仅研究，不计入十二个执行账户。

## 证据读取

先读 ../ashare-daily-review/references/tool-contract.md。
先用 ashare_review_readiness 检查已存日历、冻结快照、结算与材料。
买卖账事实优先 paper_execution_evidence（summary/trades/fills/orders/positions/cycles）；
推送与门禁用 paper_notification_ledger、paper_decision_trace；优先复用 paper_research_artifact 的既有出版结果。
当前协议对照再用 paper_experiment_report、paper_daily_outcomes、paper_candidate_shadow；
C3 用 paper_c3_events，涨停冻结归因用 ashare_review_history/snapshot/attributions。
市场先用 ashare_market_review_universe section=summary 做全部已存宇宙日K/采样分钟批统计，
再用 features/代码游标深入上涨与非上涨（含未知）对照；分开机器统计与逐股因果覆盖，不只看赢家；
价格用 ashare_price_evidence（日K、采样1m/5m），累计量额不是分钟增量。
这些工具不负责补采、扫描、结算、创建账户或回放。缺失应标未知，不通过写接口补齐。
实验报告是当前协议下的即时只读汇总，不是任意历史时点快照；每日 outcome 只读取已结算数据。
候选 shadow 标签不是已实现交易收益。当前源码不能证明历史版本当时如何决策，必须找对应版本和冻结记录。

## 复盘步骤

1. 明确交易日、阶段、观察截止时点、账户和策略/信号/执行版本；交易日使用项目日历，不把自然日当交易日。
2. 检查行情、候选、扫描轮次、确认时钟及结算完整性；区分真实零样本与缺数据。
3. 对每个账户列出：观察宇宙 → 入池 → 形态 → 确认 → 风控 → 仓位/容量 → 订单 → 成交 → T+1 可卖 → 退出。
   只报告有证据的漏斗计数；上述 MCP 不一定覆盖每级，缺项明确写不可观测，不能编造。
4. 对涨幅/涨停样本同时取当时可见的失败和未成交对照。标明事实、关联、假设，避免事后用涨幅解释全部买点。
5. 分离“未扫描、数据缺失、未入池、未确认、风控拦截、容量不足、不可成交、持仓未闭合”；
   追踪买卖条件及原因到既有策略源码/冻结日志，不把低频买入直接判为失效。
6. 按当前协议的完整交易周期计算净收益、胜率、盈亏比及尾部损失；保留费用、税、滑点、涨跌停/T+1、
   停牌和分批退出语义。一次卖出不等于一个完整周期，未退出持仓不混入已实现胜率。
7. 隔离旧版本、跨协议、控制/探针样本和不完整周期，报告排除原因及完整分母；零样本胜率为不可估计，不是 0%。
8. 给出下一次可证伪研究假设，而非单日改阈值或自动提升风险；实验交给 claw-strategy-experiment。

## 定时执行

自动化标记模式读取 ../ashare-daily-review/references/automation-contract.md。
按账户分离当天现金净流、当天卖出已实现持有期盈亏、当日闭合周期与未实现浮盈；不得混为单日净收益。
跨版本真实账事实保留，当前协议实验样本另列。当前持仓/订单投影不是历史 as-of。
未推送、追高或踏空必须联结具体股票的当时日志/通知/成交/行情；没有日志只能未知。
遍历有界分页时记录实际覆盖、未评估量与停止原因；未覆盖全量不得称“所有上涨股已研究”。

## 固定输出

- 时点与证据：请求交易日、实际覆盖区间、as-of、版本、质量、缺失/排除量、工具或源码定位。
- 十二账户表：账户/策略版本、漏斗可观测情况、完整周期数、净收益、尾部风险、未闭合持仓、主要阻断及置信边界。
- 买卖归因：选择代表性命中、错过、误报、未成交和退出案例；C3 另列研究证据。
- 改进队列：先数据/时钟/可执行性，再排序/入场/退出；每项注明假设、验证集、基线及停止条件。

不要重启服务、写交易库、补单、修改参数或替用户晋级。需要扩展读取时先审查被调函数是否隐含写入；
GET 不保证只读。离线材料遵守 docs/research-storage-policy.md，优先按日/账户有界导出，不反复复制全库。

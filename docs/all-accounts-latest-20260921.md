# A—F及A2—F2交易执行版更新（2026-09-21盘后）

## 最终交付

**22:26:15对原模拟盘后端受控发布；22:27:35逐户验收12/12账户的实际执行身份与最新源码一致，全部active及auto_buy_enabled。** 原服务PID9190→11913，仍为127.0.0.1:8000、原launchd监督者，没有另起服务、切实盘或手工下单。

本次真正修复了主A—F及E2的挂单行情混合失效优先级，并将7户执行合同升级为`primary_quote_confirmation_v2`；A2/B2/C2/D2/F2保留21:42已部署的最新`route_confirmation_v1`。不是把12户基础策略标签统一改成v4，也不是把C3研究结论无条件接入交易。

**78文件3275通过、1指定历史夹具缺失跳过；13项发布检查和3项全账户版本检查在就绪/稳定两个窗口均通过。** 仍有盘后intraday_fast missed3.940秒告警，operational_health=degraded；发布通过不代表全天业务健康、不会漏单或收益达标。

## 1. 实际修复与影响

此前primary pending先遍历必需字段，只要high/prev_close等缺失就waiting，尚未判断独立且已知的价格低于VWAP、超过回撤阈值等失效。该问题影响A—F和E2；上一轮5个connected次账户的修复并不覆盖这条路径。

现在：
- 在健康当轮行情前提下，独立原判据逐项求值，**已知失效优先于未知**；未知-only继续等待，不借旧候选价格凑数据。
- 各户原VWAP/高点回撤阈值、A的VWAP锚分支不变；静态账户/来源错配不能被缺字段遮蔽。
- high<price维持原待数据语义；A/vwap low>price保留原终态，不放宽为等待。
- 正常完整行情仍走原stable gate和各路线生成器。2688个完整、几何一致输入组合的旧新状态对照一致，非历史收益回放。
- 已知失效取消的是未成交余量；已有成交不反做，取消后恢复报价不复活旧单。未知等待不延长原TTL。
- 七户合同进入执行哈希；旧买单不得跨版本成交，旧仓不得跨版加仓，但保留原入场版本和保护性退出。
- 本轮只覆盖quote层独立判据，没有重写所有下游资金、候选及退出状态机，也没有证据将今天具体某笔漏买归因于该缺陷。

生产仅局部改：
1. `backend/app/api/v1/paper.py`：紧邻helper `_pending_primary_quote_issues`、`_pending_primary_buy_confirmation`。
2. `backend/app/paper/experiment.py`：七户primary合同进入原执行身份。

其余生产AST、settings、仓位比例、名额、TTL、费用、风控、T+1与整手规则不改。保留工作树既有并行修改，没有回滚或提交他人代码。

## 2. 发布后12户版本矩阵

原进程报告时点22:27:35.168523；各户均已创建、已启用本地模拟买入尝试。末列为完整执行身份的12位摘要，完整值保存在`after_stable_versions.json`。

|账户|当前基础算法/路线|本次执行摘要|动作|
|---|---|---|---|
|A default|paper_a_value_entry_v2|63b19b104a8e|切换primary新合同|
|B promotion|paper_b_promotion_dry_v2|20889eb4792f|切换primary新合同|
|C mainline|paper_c_mainline_v2|c2c4d7423569|切换primary新合同|
|D auction|paper_d_auction_quality_v2|9ab249af5726|切换primary新合同|
|E tenbagger|paper_e_highboard_v3|e6e7012ca95b|切换primary新合同|
|F reversal|paper_f_research_v2|0c99b26e64c2|切换primary新合同|
|A2 challenger_a|a2_momentum_retest_v1 + momentum_retest_v4|bfc8a7de6117|已最新，保持|
|B2 challenger_b|abcdef_shape_v4 / 弱开二板|b30ff4ee695d|已最新，保持|
|C2 challenger_c|abcdef_shape_v4 / 涨停记忆再启动|94aa1c2b518e|已最新，保持|
|D2 challenger_d|abcdef_shape_v4 / 竞价修复|dfe27f4445e3|已最新，保持|
|E2 challenger_e|e2_highboard_reseal_v2|e7b5b382bc2b|切换primary新合同|
|F2 challenger_f2|abcdef_shape_v4 / 高标断板收复|3961a7ed54be|已最新，保持|

- 不同路线的v2/v3/v4不可按数字大小互换，C主/C2/C3不是同一个算法。B的dry和F的research是基础身份标签，不代表当前模拟开关关闭。
- active/auto_buy_enabled只证明配置允许尝试；真实扫描还需交易时段、当前数据、原买点、资金、风控与可成交盘口通过。
- 所有户仍用pending_buy_validity_v2。5个connected保持route_confirmation_v1；7个primary新增primary_quote_confirmation_v2。
- 新版本尚无下一交易日盘中证据，不补写扫描日志/买单。历史NAV/亏损/订单/持仓保留；按当前版本统计的样本分组变化不等于把历史亏损清零。
- A既有冷却后分层、A2/B2/C2既有目标仓位补足逻辑此前已部署，不把它们冒称本次新增。

## 3. 验证与发布证据

证据根：`outputs/all_accounts_latest_20260921/`。

### 修复前核验与独立审查
- 22:03–22:08先核对当时12户运行身份与源码一致，没有遗漏加载。之后独立审查发现上述primary缺口，才进入实际修复。
- `final_verification.json`的already_latest_no_op是**修复前阶段证据**，不是最终交付；转向记录见`scope_transition.json`。最终以`deployment_result.json`及`after_stable_versions.json`为准。
- 两名审查者的`main_review.md`、`secondary_review.md`保留原发现；最终只读patch review无阻断本次最小修复的问题。
- B/C/D上游晋级模型身份纳入执行指纹的额外建议留待后续；原候选run_key仍有保护，没有据此认定当前生产模型错配，不混入本次quote合同。

### 红绿与完整回归
- `primary_repair/red.*`：原函数51失败/342通过，含真实deferred部分成交后mixed仍waiting；相同393项首绿通过。
- 父审发现首版low>price曾意外从终态变等待，5项红测试复现后收敛，保留失败记录，不改原策略门槛。
- 新文件`backend/tests/test_primary_confirmation_contract_20260921.py`最终433项全通过，含E/E2×3缺字段的真实排队消费六例、unknown等待→mixed取消→恢复不复活。
- `backend/tests/test_paper_execution_signal_version.py`本次新增30项：七户合同作用域、旧身份不可复用、7户×standard/continuous真实service版本门。该文件共55项通过。
- 最终78文件：3275 passed、1 skipped、0失败/错误、3276唯一JUnit身份；pytest338.53秒。唯一skip为`test_20260914_frozen_13_accounts_cash_fees_and_inventory`缺指定历史fixture，26条既有警告保留。
- 隔离临时DB、禁调度/推送、33次INET连接尝试被阻断；源码与测试文件在全组运行及发布前不变。部分旧审计可只读历史证据，不宣称零历史库读取。
- 部分成交测试的broker/risk为隔离替身；不得单独当成真实现金/收益回放。真实线上账务保全由下项只读前后摘要另验。
- 初始版本探针两次解释器定位失败保留；修正为独立测试环境计算期望身份再与原进程比较，没有执行未知shell前缀、没有触及原库。

### 原服务验收
- 22:26:15.975883一次SIGTERM，原KeepAlive正常重启PID9190→11913；22:26:21就绪、22:27:35稳定。唯一8000监听、52任务及watchdog注册。
- 原私有配置摘要和Alembic版本不变；181交易账本全字段、364fill全字段、88持仓全字段、2341订单核心字段、353正式预测run受检字段摘要不变，12户现金不变、无活动挂单、6项不变量及现金对账差异均0。
- 12/12运行版本=最新源码期望，12/12启用，仅目标7户身份旋转，5户保持；两个验收窗口均通过。
- 仍记录intraday_fast missed3.940秒，22:27:02.176340观测，原计划22:26:58.236244。重启后的current_process_only监控不能抹去先前告警，盘后quote=not_expected不能证明盘中时效。
- 无进程内导入模块hash端点：验证的是测试源码SHA、正常新进程启动和实际执行身份，不能冒充全库逐字节不变或读取了所有内存字节码。
- 未改前端/API字段，未做前端build或冒称UI全验收。

## 4. 未混入本次发布的事项

- 强势推进/整理突破/水下修复研究尚未变成12套新选股算法；C3仍不接交易。
- 仍为12个各自5万元独立模拟账户，不是单个5万元组合；未自动合并、提高仓位或用当天赢家决定权重。
- 飞书独立入队补偿、限频过期仍未解决。
- 收益、回撤和下一交易日真实消费/成交时效未获证明。此次交付是**全账户最新交易执行版及已复现缺陷修复上线**，不是稳定盈利承诺。

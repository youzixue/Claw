# 因子与策略证据链修复进度（2026-09-14）

## 授权与边界

用户已采纳午间复盘建议，要求按顺序修复。文件工具实际写入成功；不要再根据旧会话的只读提示推断当前文件不可写。各业务工具权限仍独立，以调用结果为准，不绕过拒绝。

- 保护工作树中原有大量未提交/未跟踪文件；不得整文件重写调度器、晋级预测、牛股雷达或模拟盘路由。
- 不直接放宽生产买入阈值、T+1、可成交性、仓位和风控；不自动调权或晋级；不下单。
- 不改写历史市场/预测证据。本次复盘市场边界仍是2026-09-14 11:30，证据截止11:30:18；日K仅截至9月11日。
- 源码修复和生产部署分开验收；当前未主动重启/部署后端，运行版本尚未验收，不能宣称运行服务已经采用新代码。

## 分阶段目标

1. 行情连续性、调度和策略覆盖。
2. 形态确认与可执行契约的一致性。
3. 因子缺失值、真实前瞻收益IC、新闻实体及首次可用时间。
4. 预测漏斗诊断与模块共享因子证据；新增评分仅研究/影子验证，不直接改交易决策。
5. 相关pytest、涉及前端时的构建及整体验收。

## 已核实事实

- 上午归档轮次在11:01:45→11:06:52和11:07:15→11:12:15分别有306.71秒、299.72秒间隔；逐轮quality_status=ok不证明跨轮连续。
- A2上午有2688只股票因源报价间隔超90秒进入coverage_blocked；09:31一批源间隔93/94秒即触发，不能仅提高门槛来补救。
- 修复前scheduler._publish_quote_round是latest-only槽位，交易和重型影子扫描结束前可合并掉中间轮次。第2批已为A2保留独立有界证据队列；这与采集本身断档是两类问题，不能把所有间隔都归因于该槽位。
- A2 confirmed后为终态，同一日期/股票/版本事件去重；真实缺口后不能把新鲜报价冒充原有首次回踩连续路径。
- 宝鼎科技确认事件量比5.16，规则上限5.0，执行层随后拒绝；确认和可执行语义须核对，但不可凭当天涨停直接放宽上限。
- B执行日志09:35漏斗：路线池33，交易资格非观察6、actionable6、概率门槛通过0。该日志不替代完整事前预测快照。

## 第1批已完成（源码，未部署）

修改文件：

- backend/app/data/quote_round.py
  - 新增quote_round_continuity纯审计函数，复用TRADE_SESSIONS与local_clock。
  - 计算已提交轮次清单时钟之间的交易时段间隔，扣除午休、竞价后计划停段。
  - 区分unknown/new_trade_date/invalid_clock/continuous/gap。
  - 写入既有component_watermarks_json.source_quote_continuity，无新增表或历史回填。
  - 明确scope=committed_rounds_only，并保留逐股票源时钟检查要求。沿用现有轮次时钟口径，不冒充真实DB提交耗时证据。
  - 不改变单轮quality_status；新鲜报价继续服务持仓风控，路径策略仍按原规则失败关闭。
- backend/app/data/scheduler.py
  - 仅局部增加读取同日上一已提交轮次、传入审计函数以及连续性告警。
  - 未修改原有交易执行顺序、重入锁、采集频率、数据源字段和任何交易阈值。
- backend/tests/test_momentum_retest_shadow.py
  - 增加93/94/306秒断档后不得静默重新武装的回归测试。
- backend/tests/test_quote_round_fresh_coverage.py
  - 增加正常/90秒边界/93秒/307秒、午休、竞价间隔、无基线、跨日、重复/未来时钟测试。

验证：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest tests/test_quote_round_fresh_coverage.py tests/test_quote_round_execution.py tests/test_quote_round_minute_integrity.py tests/test_momentum_retest_shadow.py tests/test_paper_position_risk_transactions.py -q -p no:cacheprovider
```

结果：55 passed，2条既有依赖/拼接警告。conftest先于app导入将DATABASE_URL固定为独立临时库，并用autouse守卫断言路径，未触碰生产库。

`git diff --check -- backend/app/data/scheduler.py`提示该文件原有大范围改动中的12处行尾空白（3050、3213等），不在本次修改的局部块内；按保护并行改动原则没有顺手清理，也不宣称全文件diff检查无警告。

## 第2批（2026-09-14，goal round 4；源码未部署）

本批修改范围：scheduler.py、config/settings.py、paper/momentum_retest_shadow.py、test_paper_position_risk_transactions.py、test_momentum_retest_shadow.py、test_scheduler_kline_fill.py。scheduler和settings是共享文件，均为局部增改；未清理其他并行改动。

- A2纯行情证据不再跟随交易latest-only槽位丢帧：保留默认6个待消费前向轮次，另保留最多1个提交中的失败重试帧。
- 只消费原始采集时点不晚于当前交易payload的帧；消费期间新到轮次留到下次。重复/倒序轮次不回退A2时钟，不跨日重建旧路径。
- 失败提交先重试，不能让后续帧越过；风控失败仍采证但不新增风险，watchdog交易去重不再跳过A2证据。
- 队列溢出记录丢失首尾round_id和数量。已知丢帧即使剩余报价间隔低于90秒，也不能冒充连续路径；当日未终结及后到股票保守阻断，已确认的历史事件不改写，新交易日独立重置。
- 质量降级轮次仍禁入场，并记录consumer_coverage_loss；不以部分健康报价静默恢复首次路径。消费缺口与源quote_gap可区分。
- 只对A2纯行情状态机保序；C2等依赖板块/资金的重型扫描和隔离账户执行仍只运行当前轮次，不重放旧订单或用当前资金倒填旧信号。
- _tencent_spot_collect提交后先无await发布，再await交易日历通知；已提交行情不再额外等待该日历查询才能唤醒交易。
- 新事件增加coverage_policy=explicit_consumer_loss_v1用于区分新旧覆盖语义。不改历史事件、v3策略阈值或现有quote配置哈希。
- 设置新增PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES=6，这是内存容量，不是买入条件；修正旧“未接模拟盘”注释，实际A2消费关系已在源码核实。

验证覆盖：轮次合并、消费期间新发布、失败提交重试、有界溢出、重复/倒序/跨日、watchdog去重、降级轮次、短间隔真实丢帧、缺口恢复后不得重武装、已确认事件不可改写、空帧、恢复及次日日切；另用临时SQLite和真实A2状态机检验6帧可产生armed→candidate→pullback→confirmed、2帧溢出只能coverage_blocked。新增A2缓存集成测试仅采证，不调用交易执行器；扩展回归中的交易测试也限定在pytest隔离库。

结果：bash-544 66 passed；bash-547 68 passed；bash-548 扩展9组测试201 passed。最后补充coverage_policy字段后的最终源码复验bash-551：201 passed，37.47秒，2条既有警告。上述后台测试任务均已收取，exit code 0。

扩展命令：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false python3 -B -m pytest tests/test_paper_position_risk_transactions.py tests/test_momentum_retest_shadow.py tests/test_quote_round_fresh_coverage.py tests/test_quote_round_execution.py tests/test_quote_round_minute_integrity.py tests/test_scheduler_kline_fill.py tests/test_strategy_iteration_shadow.py tests/test_strategy_iteration_challenger.py tests/test_strategy_iteration_confirmation_segments.py -q -p no:cacheprovider
```

QUOTE_ROUND_ARCHIVE_ENABLED=false只对测试进程生效，防止调度单测启动生产目录归档。测试DB仍由conftest和各tmp_path隔离，不更改运行配置或市场数据库。未改前端，本批无前端构建。

交易影响边界：可以恢复“已经采集但被消费合并丢掉”的真实A2确认，也会更严格阻断显式未知路径；仍必须经过当前报价、下一轮撮合、T+1和风控，不代表这些股票都应该买到，更不承诺收益改善。容量与持锁耗时仍需部署后的前向负载验收，源码测试不证明源采集断档已经解决。

## 本轮新增的运行日志证据（只读，仍限定上午）

读取logs/backend-uvicorn.log：
- 5578902：11:01:47.055入库5218只、quality=ok，对应collected_at=11:01:45.314636，DB路径结束约晚1.74秒，并非本次5分钟断档都发生在这次commit。
- 5578909→5578911：11:01:48.313之后到11:06:49.093，多业务日志共同空窗约301秒；随后快频、竞价、指数、overview、watchdog、派生等一起出现missed-by。
- 5579023→5579024：11:07:33.128到11:12:10.810再次出现约278秒共同空窗；5579029腾讯行情调度missed by 29.253623秒。
- 5578923、5579046：恢复时异动扫描均报告stale=5218、fresh=0，支持“源采集/进程调度整体停顿”，不是仅A2消费者合并。
- 上午腾讯行情明确misfire记录：09:16、09:18、09:25、09:31（20.285377秒）、10:14、10:40（18.908701秒）、11:12。现有coalesce=True/max_instances=1/misfire_grace_time=10，因此部分延迟采集被跳过。
- 日志可证明多个任务共同停顿与misfire，尚不能只凭这些区分同步CPU/IO阻塞、进程/主机暂停或日志阻塞，更不能武断归因SQLite；本窗口未找到带该时点的database-is-locked记录。
- 下一步可评估“保留coalesce/max_instances，允许有界延迟的实时重新采集”是否减少额外跳轮；这是调度参数，不是放宽交易源时钟门禁，但必须先核对原有10秒配置意图和增加反例测试。不要仅看到gap就把A2的90秒改大。

## 第3批（goal round 5）：休眠根因与确认/执行契约

### 已核实的休眠根因

只读执行pmset -g log及读取本机caffeinate手册，系统日志与Claw上午日志对齐：
- 11:01:01：Clamshell Sleep（合盖），2秒后进入45秒DarkWake。
- 11:01:48→11:06:48：Maintenance Sleep 300秒。
- 11:07:34→11:12:10：Maintenance Sleep 276秒；11:12:10由lid/UserActivity唤醒。

因此这两段长空窗的直接原因已确定为主机休眠，不能再泛称“可能数据库卡住”。不代表更早09:31/10:40的短misfire全部同因。系统证据全文保存在本轮工具输出临时文件：
/var/folders/wy/q152mnjx51x5tfc93sfkxnkr0000gn/T/dsh-subprocess-Nzal4v/dsh-subprocess-6770-16-452a27d99879-stdout.log（22769、22775、22801、22807、22826、22833行）。

**重要运维边界**：caffeinate不能保证阻止合盖或手动休眠。交易采集机必须保持开盖，或使用Apple支持的外接屏幕/键鼠/电源闭盖配置，或迁至常开主机。不能靠软件补造休眠期间的分时，也不修改全机pmset设置。参考：[caffeinate手册](https://manpagez.com/man/8/caffeinate/)及[Apple闭盖外接显示器要求](https://support.apple.com/en-gb/102501)。

### 进程级防休眠（源码未部署）

- 新增backend/app/core/process_awake.py，在macOS调度器启动后请求/usr/bin/caffeinate -s -w <backend_pid>。
- 只在AC供电生效，不阻止显示器休眠、不唤亮屏幕、不在电池供电强制保持唤醒；调度器停止时terminate/wait，必要时有界kill，后端退出时PID绑定自动解除。
- 默认SCHEDULER_PREVENT_IDLE_SLEEP=True；可由环境变量关闭。非macOS跳过；命令失败/提前退出明确暴露，不把process_running冒充已阻止一切休眠。
- scheduler.get_pipeline_runtime_status新增process_awake状态，无新路由或前端改版。没有实际启动防休眠进程、重启后端或修改宿主电源配置。
- tests/conftest在导入app前固定关闭该功能；新test_process_awake全部使用假子进程，并验证禁用、Linux跳过、幂等、PID绑定、早退、命令缺失、超时回收、scheduler提前停止清理。
- 新增276/300秒旧报价不得用于风控/开仓的回归，沿用现有唤醒后源时钟和90秒路径断档检查；不放宽misfire或买入参数。

### A2共享流动性契约

修改momentum_retest_shadow.py和strategy_iteration_challenger.py：
- 复用同一momentum_liquidity_gate_issues函数检查量比上下限、累计成交额、盘口失衡、撤单比例；候选、确认、实时执行都调用，不再各自定义另一套数值规则。
- 量比5.16大于既有5.0时不会发出confirmed；上限未放宽。
- None/NaN/Infinity/非法字符串不再当作盘口0或撤单0等“中性好数据”。新事件包含liquidity_contract=momentum_liquidity_v1；非有限行情叶子输出null，保留浏览器可解析JSON。
- 尚未confirmed时流动性未通过追加一次confirmation_screened，保持原首次回踩状态与等待窗口，可在同一真实路径质量恢复后确认；不伪造一次confirmed再让执行器拒绝。
- 执行器记录execution_liquidity_contract/完整issue code/recoverable标记。流动性暂缺只能等下一新鲜轮次；已确认后出现真实规则违反仍终止该信号，不延长事件原始时效。
- 显式max_withdrawal_ratio=0不会被“or 0.5”误替换；旧部分规则快照的settings回退保持兼容，新事件仍完整冻结规则。
- 原峰值锚点、真实盘口撮合、价格漂移、created_at不得晚于当前轮次、事件最大时效、T+1、账户风险/仓位/预算全部保留。未改A-F Champion评分或任何阈值。
- 修正隔离测试的“合格A2”样本，使其真实包含amount/withdrawal_ratio；另补候选→确认→执行同数据矩阵及暂缺数据跨轮恢复测试，而不是删掉缺失值门禁来迁就旧测试。

验证：首次导入编辑匹配失败引发NameError（bash-552）已补齐导入，bash-553 55 passed。bash-556提示旧累计成交额文案断言失败，已保留兼容文案；bash-557提示旧买点测试不可变A2报价缺amount，已修正合格fixture并保留缺失反例。最终bash-558：**294 passed，47.52秒，2条既有警告，exit code 0**。全部后台任务已收取；前端未修改。

最终扩展命令（均为隔离测试，无生产订单/市场库写入）：

```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false python3 -B -m pytest tests/test_process_awake.py tests/test_paper_position_risk_transactions.py tests/test_momentum_retest_shadow.py tests/test_quote_round_fresh_coverage.py tests/test_quote_round_execution.py tests/test_quote_round_minute_integrity.py tests/test_scheduler_kline_fill.py tests/test_strategy_iteration_shadow.py tests/test_strategy_iteration_challenger.py tests/test_strategy_iteration_confirmation_segments.py tests/test_challenger_continuous_boundaries.py tests/test_paper_signal_policy_isolation.py tests/test_paper_buy_point_hooks.py -q -p no:cacheprovider
```

本批修改清单：backend/app/core/process_awake.py（新）、backend/app/data/scheduler.py、backend/app/config/settings.py、backend/app/paper/momentum_retest_shadow.py、backend/app/paper/strategy_iteration_challenger.py、backend/tests/conftest.py、backend/tests/test_process_awake.py（新）、backend/tests/test_paper_position_risk_transactions.py、backend/tests/test_momentum_retest_shadow.py、backend/tests/test_strategy_iteration_challenger.py、backend/tests/test_challenger_continuous_boundaries.py及此文档。共享scheduler/settings/conftest仅局部增改，无生产配置文件修改。AC供电下运行调度器可能增加保持唤醒的能耗，可通过SCHEDULER_PREVENT_IDLE_SLEEP=false关闭。

## 第4批（goal round 6）：旧确认防补证与全48因子输入契约

本轮重新读取工作树、目标、进度和相关源码后执行。文件edit/write均实际成功；没有通过替代渠道绕过权限拒绝。

### 旧A2确认不能借用后续行情补证

- strategy_iteration_challenger.py在当前报价复核之前，用既有共享流动性契约校验不可变confirmed.snapshot_json.quote及原规则。
- 原始量比5.16、缺amount/量比/盘口/撤单或真实越界，追加skip_terminal，execution_block_class=invalid_confirmation_evidence。原确认永久缺证与“原确认合格但当前报价临时缺证”分开：后者仍可在原时效内等下一轮。
- 不改原PaperShadowEvent，不给旧事件补liquidity_contract标记；确认/执行契约及issue只追加在执行诊断中。
- 新增6项临时库回归：旧源证据不合格+当前完整仍不下单、下一轮不能复活、事件原文不变、尚未进入风险提交。bash-559：139 passed，1条既有依赖警告，exit 0。

### 因子缺失值与窗口完整性

修改base.py和现有10个分类模块，未创建平行因子算法：

- FactorResult统一将None/NaN/Infinity/布尔/非法类型归一为value=null、confidence=0、rank/pct=null；因子自有嵌套元数据中的非有限数也变为null。有效0仍是0，不被当缺失。
- 每个现有calculate显式使用同一validate_factor_inputs装饰器，直接调用和FactorEngine批量调用都有相同门禁。分类声明required_context、数值范围/枚举及真实依赖窗口。
- 既有公式所需的情绪、板块、基本面、晋级、新闻、融资等输入不再由缺省0/1/0.5/修复/启动产生伪观测；缺失和非法字段可从input_issues定位。未知情绪/生命周期不再自动映射为修复/启动。这里保留原牛股生命周期因子枚举，没有偷换成板块七阶段口径。
- pandas求和、均值、RSI比较或EMA不得跳过真实依赖窗口内缺失值继续出分。有限窗口不依赖更早无关脏数据；递归MACD/KDJ检查实际使用的完整历史。融资5日均值不能跳过缺失一天或用kwargs替换残缺的已提供窗口。
- 20日均量比较需要20个历史点+当前点，volume_spike/breakout_energy不再将20条总记录误当完整20日历史。零量分母不退回“量比1”；价格非正、数量字段负数降级。没有更换MA/MACD/RSI/KDJ/BOLL等既有公式或调参。
- 新闻明确且一致的1h/24h零计数才返回零热度；有新闻却无重要性、窗口矛盾或负计数均缺证。未接入真实新闻实体和首次可用时钟前，此契约不代表新闻已满足PIT。
- 首板时间缺失/非法/非交易时段不能默认尾盘评分。晋级经验分保持原数值，新增semantics=heuristic_score、calibrated_probability=false及明确说明，不冒充生产校准概率。
- 截面排名排除无值/非有限值/零置信度，并保留反向因子异常结果的方向。未改并列排名或百分位公式。
- 返回元数据input_contract=factor_input_v1是计算输入契约版本，不是历史数据已可用的证明。旧FactorValue行没有因此获得时点或协议标记，未调用存储服务重算/覆盖历史。

测试新增backend/tests/test_factor_input_contract.py，覆盖全部48因子空输入、完整输入保持既有公式、逐依赖字段/上下文None/NaN/Inf/非法类型矩阵、零值、范围矛盾、未知枚举、窗口内部断点、20+1边界、融资残缺窗口、严格JSON、正反截面排名及异常方向。bash-560：657 passed。首次扩展15组bash-561：957 passed，50.59秒，2条既有警告；随后补数值溢出和午休非法封板时间边界，最终源码复验bash-562：**959 passed，55.15秒，2条既有警告，exit code 0**。bash-559/560/561/562均已收取。

本批文件：backend/app/paper/strategy_iteration_challenger.py、backend/tests/test_challenger_continuous_boundaries.py、backend/app/factors/{base,technical,fund_flow,sentiment,sector,fundamental,breakout,promotion,lifecycle,news,margin}.py、backend/tests/test_factor_input_contract.py（新）及此文档。未改前端、ORM或生产配置；未重启部署、写生产库、自动调权或下单。影响研究因子值/可用覆盖/排名和A2旧缺证事件的开仓资格；不代表收益必然改善。

### 下批IC/页面契约已预读，尚未修复

- evaluator.py:267–281确实是相邻factor_rank自相关，必须改为同日因子值对真实后续收益的横截面Spearman，而不是给旧指标改名继续当IC。
- 官方定义与项目注释一致：[Alphalens](https://quantopian.github.io/alphalens/alphalens.html)、[源码](https://github.com/quantopian/alphalens/blob/master/alphalens/performance.py)、[SciPy](https://docs.scipy.org/doc/scipy/tutorial/stats/hypothesis_spearmanr.html)。未来收益仅用于已成熟标签的离线研究，不可当作当时输入，IC也不等于可交易收益。
- FactorValue目前无computed_at/as_of/来源版本，存储服务会原地覆盖(date,code,name)；FactorEvaluation唯一键(name,date)且无协议，重复评估还会冲突。需要先设计可区分legacy_unknown与新前向证据的迁移/幂等和读写边界，不能重标/覆盖旧值或旧伪IC。
- Alembic当前已读到027_fund_order_breakdown，下一批定义迁移前再次检查head以避开并行任务。db/session.py目前无factor自动补列。
- GET /factors/evaluate和单因子GET会写库；前端onMounted自动调用，并错误期待数组（后端是results对象）。后端应读写分离，再局部同步页面，不能只改UI掩盖。
- 页面方向将±1与字符串positive比较，导致正向也显示反向；pct实际0–1但阈值用70/30，零置信度/胜率被||语义显示成--；需要契约修复而非整页重写。前端已有并行样式改动，保留四个Tab。
- factor_scheduler.compute_and_store_factors只计算前100只/30条StockDaily、不注入外部context，也未调用截面排名。缺失契约修复后会真实暴露更多未接入数据；不可再宣传全市场48因子均已计算。
- 实际金融时间对齐、复权/公司行动、停牌及标签成熟度仍需核实，不能用下一条股票记录代替下一交易日，也不能把当日最终收盘用于上午复盘。

## 第5批（goal round 7）：真实收益IC、追加式评估与页面契约

### 计算与证据口径

- evaluator.py不再读取factor_rank计算“IC”。新协议spearman_next_session_formal_close_v1，按同日股票截面计算原始factor_value与下一交易日正式收盘收益的Spearman；原始IC保留原符号，另给direction_adjusted_ic_mean，衰减按因子方向调整。
- 复用promotion/outcome_evidence.py的next_recorded_trade_day、formal_outcome_bar_error、_is_completed_outcome_date，不新增另一套日历/正式日K准入规则。
- 收益来源StockKline正式ths/tencent_close；拒绝临时来源、无量/无价/停牌和前后prev_close价格链不一致（原共享0.011阈值），不混用StockDaily与StockKline来拼一条收益。
- 指定as_of_at只允许当前或过去；当地15:10前不查询当天最终日K或当天日因子。查询只读已记录TradeCalendarModel，不触发可联网/写库的日历补全；缺失日期不能跳到下一条已有股票K线。
- 每日有效股票至少10且因子/收益非恒定；NaN/Inf/不同资产索引/重复索引不冒充有效样本。单日IC标准差/IR不足返回null，零方差IR不伪造0。
- 逐日配对数、排除原因、无效截面、输入数、截止时间均可追踪。未成熟尾部不属于衰减样本，已成熟但缺证的日期仍阻断连续性。边界测试发现原reversed(Series切片)按标签取值会KeyError，改为iloc[::-1]，不改变衰减阈值。

**限制不隐瞒**：FactorValue没有首次计算/可用时间或数据版本，因子历史仍legacy_unknown；正式日K也是读时材料，不能证明历史到达时间。新结果始终point_in_time_verified=false、promotion_eligible=false、automatic_weight_update=false，仅供研究。IC不是可成交收益，尚未计费用、滑点、T+1及涨跌停限制，覆盖池不是全市场；不能以此直接调权/晋级。

### 保留历史与读写分离

- models/factor.py新增FactorEvaluationRun；迁移028_factor_evaluation_runs接027，仅建新表/索引，不更新旧FactorEvaluation/FactorValue行。
- 新运行冻结最小因子值、正式日K叶子、记录日历、协议、方向、截止时点到input_json，SHA256输入标识；result_json独立保存。相同材料/时点幂等，材料后续修订产生新运行，原运行可独立复算。
- FactorStorageService.save_factor_values遇到旧(date,code,name)不再原地覆盖；只增加不存在的旧格式行，不给旧数据补造首次时钟/新协议标签。版本化前向因子采证仍待后续阶段，不把该旧表称为PIT仓库。
- GET /factors/evaluate、/evaluate/{name}只读保存报告，支持日期/as_of_at；非法/未来日期422。显式计算复用原POST /eval/evaluate/daily，增加as_of_at透传。
- 研究评估不调用权重优化器，optimized_weights为空；有效评估数按真正有收益IC的因子数统计，不将48个空结果算作成功。
- 旧排名自相关仍留在旧表，并在返回中标legacy_not_ic/legacy_rank_autocorrelation；不会填入新IC/IR字段，也不能驱动衰减诊断。
- performance.factor-eval和factor_scheduler.get_decaying_factors复用同一新报告，不再从旧表挑“正常/衰减”。报告查询只取结果列，避免把大份input_json载入页面查询。

### 前端局部同步（保留已有四Tab和并行样式）

- 因子方向按数值±1展示，不再全部误显示反向。
- 评估读取results对象，区分真实收益IC·仅研究、旧排名自相关·非IC、样本不足；缺评估不显示“正常”，所谓胜率改名IC正值占比。
- 百分位0–1正确换算百分比，0值/0置信度正常显示；缺失值仍为--，显示input_issues原因。
- 打开页面和刷新只GET；“运行研究评估”才显式POST。计算错误清空旧结果并提示，不悄悄保留上次股票分数。
- 局部表格单元格覆盖水平padding/nowrap，解决新增口径列下数值/状态断行或截断，不改全局样式。绩效页同步旧口径及未知衰减状态。

### 验证与文件

- 新test_factor_evaluation_protocol.py：排名不变而收益排序相反时IC=-1；原始/反向IC、上午截断、正式来源/停牌/价链、日历缺口、幂等、冻结材料复算、旧值不覆盖、GET零提交/零计算、显式POST不调用调权、日期422、空/恒定/非有限/缺索引、衰减成熟尾部、迁移重复升级/降级不碰旧行。
- bash-563：679 passed；bash-565：979 passed；补保存/日期边界后bash-567：982 passed。补充成熟衰减长序列时bash-569暴露上述pandas索引错误（1 failed,682 passed），已修复；最终扩展bash-570：**983 passed，61.86秒，2条既有警告，exit 0**。随后补有效评估数的正常/空样本断言并独立复验最终协议测试：**25 passed，5.66秒，1条既有依赖警告，exit 0**。本轮bash-563至571后台任务全部收取，无运行中遗留任务。
- npm run build已成功（bash-564、568）；5项Playwright隔离交互测试已通过（bash-566、568）。截图检查又发现全局td padding挤压数字，已仅覆盖因子评估表并增加数值不得截断断言；最终bash-571：**npm run build成功，5项Playwright通过（3.3秒），exit 0**；仅既有大chunk及NO_COLOR警告。再次直接查看截图，IC数字/正常/待评估均完整显示。
- Playwright e2e/factors.contract.spec.cjs使用刚构建的dist，由请求拦截加载本地文件；测试API全部明确fixture，WebSocket关闭，其他来源abort。没有访问运行Claw API、运行研究评估或启动替代服务器，不把测试fixture当成行情恢复/收益证据。
- 截图frontend/test-results/factors-contract-evaluation.png为隔离样例，需按最终构建复验。前端局部git diff --check通过；原共享文件其他改动不清理。

本批源码文件：backend/app/factors/evaluator.py、backend/app/models/factor.py、backend/alembic/versions/028_factor_evaluation_runs.py（新）、backend/app/risk/factor_scheduler.py、backend/app/api/v1/{factors,eval_scheduler,performance}.py、backend/tests/test_factor_evaluation_protocol.py（新）、frontend/src/views/factors/Index.vue、frontend/src/views/performance/Index.vue、frontend/src/api/index.js、frontend/e2e/factors.contract.spec.cjs（新）及此文档。构建更新frontend/dist及自动声明产物；未覆盖共享样式/其他页面改动。

部署仍未验收：未执行生产Alembic、重启后端或运行生产评估/下单；只有临时SQLite迁移和隔离浏览器验证。前后端及028需要配套加载，不能仅凭新页面就宣称运行旧后端的GET已经无副作用。

### 下批新闻源证据已复核（未修）

- FinanceNews目前只有crawl_time及nlp_analyzed_at，无不可变first_seen/内容版本。
- news.engine._save_raw_to_db:246与_save_to_db:303刷新crawl_time，旧原始正文和NLP结果被更新。
- news.catalyst.load_direct_stock_catalyst_map按publish_time窗口及related_codes，缺首次可用/NLP实体校验时点。
- 下一批需沿用现有news/catalyst/entity管道做确定性名称-代码验证、首次到达/分析可用时钟及内容版本，历史未知不得回填；然后接共享因子证据与预测漏斗。

## 下一步（未完成）

- 首要长空窗根因已核实，进程级防休眠不能代替开盖/常开主机要求；部署后需验证process_awake状态和真实电源断言，不能声称合盖采集已解决。
- 第2批已解决A2已采集帧被latest-only合并的问题；源采集缺口、重型C2等路径覆盖仍需独立诊断，不宣称本批覆盖所有策略。
- A2当前候选/确认/执行流动性契约已统一，第4批也校验旧不可变confirmed原始源报价；不回填旧证据。
- 执行器已有created_at<=now筛选、原observed_at最大时效、价格漂移和不可变峰值锚点；这些不是为了补买可以移除的门禁。其他路线的重型覆盖、静态价带矛盾与失效分类继续逐项核实，不强制成交。
- 第4批已修复48因子输入/缺失契约，第5批已完成真实收益IC、独立追加评估、GET读写分离及页面语义。旧FactorValue仍缺PIT证明，新前向因子证据、新闻实体/首次可用时间及模块接入仍未完成。
- 如需改全局quote配置哈希或策略版本，须说明对旧委托/持仓版本的影响。当前新增审计字段内自带policy版本和阈值快照，未切换运行配置。
- 事前预测原始快照此前MCP访问受限；用户本轮再次明确授权后，若对应工具仍拒绝，仍不能用另一渠道绕过。

## 并行审查

只读子代理586c3636-a38a-4c62-8cac-198ea9b07a19受托审查采集/调度，未拥有文件写入权；第4轮尚未收到可验收报告，当前不依赖其结论。父会话拥有本文件列出的修改范围。第1批后台任务已收取：bash-538（25 passed）、bash-539（55 passed）；第2批任务结果见上。

## 第6批（用户恢复可写权限后，源码及本批回归已验收，未部署）

本轮实际确认cwd为/Users/youzix/WorkBuddy/Claw，edit/write成功，恢复原被阻断目标。不再把旧只读限制当作当前阻断；不修改生产数据库、不执行生产迁移/重启、不下单。

### 新闻旁路与模块追踪

- promotion._load_news_catalyst_context_map不再直接查可覆盖FinanceNews补板块候选，只消费共享load_direct_stock_catalyst_map。
- 暂停sector_inferred旁路：StockSectorMapping虽有可变observed_at，SectorPersistence只有交易日期，旧查询还关联当前StockSpot。新闻自身版本校验不等于这些成员/强度/行情已有完整历史可用性证据。prediction_semantics.news_evidence_gate明确blocked/sector_context_unversioned，不把未知说成零新闻。
- 其他主线、竞价路线和生产阈值不变；旧sector查询保留为非PIT研究辅助函数，但不再由新闻候选调用。
- 将共享news_evidence的自有JSON元信息保留到晋级probability_factors/reason_snapshot，以及雷达DragonHeadResult/API。原预案main_wave_stats已有共享催化透传，复用而非另做因子算法。
- 元信息只解释既有本次输入；旧候选无证据仍为空，不补协议、不修改评分公式、不查询当前新闻来补历史预测。

新闻版本/实体/分析引擎与概率合同/执行门禁按模块委派；父会话负责旁路收口、共享证据消费者、实际学习概率入口、响应诊断和联合验收。收尾时已要求两子代理停止编辑，只收取已有任务并交接；以下测试结果均为父会话实际收取结果，不用未收到的子代理结论代替验收。

额外核实到实际概率生产入口_apply_promotion_learning_to_candidates在入库前就会用_safe_float把缺失洗成0，因此父会话在此局部前置validate_probability：原始probability及显式提供的empirical_probability/avg_predicted_probability拒绝None/布尔/字符串/非有限/越界，不等到入库才验已经被默认化的结果。缺省统计字段仅在完全不存在时沿用原回退，合法零值与既有贝叶斯/logit平滑不变。未擅自将旧生产器切为新p_*协议，deployment overlay亦未改动；新合同与显式legacy兼容须分开解释。

### 已运行验证

- 系统默认/usr/bin/python3没有pytest，首个任务bash-1明确失败；改用机器现有/usr/local/bin/python3.11，不安装/改写全局依赖。
- bash-3收集时撞到新闻模块正在修改中的导入，报NEWS_EVIDENCE_PROTOCOL缺失；不是已完成源码的验收。后续已能正常导入。
- bash-10：test_promotion_news_evidence（当时4项）、test_factor_input_contract、test_factor_evaluation_protocol，**672 passed，1条既有依赖警告，7.95秒，exit 0**。
- bash-12：两组tenbagger测试及quote_round/momentum_retest/position风险/process_awake/scheduler共9组，**431 passed，2条既有警告，18.72秒，exit 0**。
- bash-29：新增概率生产入口缺失/非法矩阵及完整promotion API，**245 passed，1条既有依赖警告，21.77秒，exit 0**。
- bash-32：包括真实临时数据库新闻接收→晚到NLP→历史cutoff重读→晋级/雷达共用证据的最终consumer测试，**5 passed，1条既有依赖警告，1.90秒，exit 0**。
- bash-35：新闻版本/催化/API/清洗/抓取、晋级消息/学习概率/持久化概率/台账/快照身份/版本/API/延迟/导入共14组，**448 passed，1条既有依赖警告，32.07秒，exit 0**。
- bash-43：模拟盘概率合同/API/策略隔离/轮次候选扫描/候选诊断，**260 passed，1条既有依赖警告，37.08秒，exit 0**。
- bash-47：纳入持久化诊断接入及最终paper新闻证据空值/复制合同后的19组联合复验，**709 passed，1条既有依赖警告，66.02秒，exit 0**。该结果覆盖最终本批新闻、晋级概率/台账/API与模拟盘消费者源码。
- 本轮父会话启动的后台测试全部已收取，无遗留运行任务。本批未改前端，不宣称已部署；完整48因子与空批次落账仍未完成，目标保持active供后续阶段继续。

### 新闻版本与概率消费的本批实现

- 新增NewsContentVersion与NewsAnalysisVersion、029_news_evidence_versions迁移，记录真实接收/内容采证/NLP完成/分析可用时钟、内容及结果hash和协议；相邻同正文/同结果重抓保留首次时钟，A→B→A保留3个前向版本。ORM禁止update/delete，SQLite新建表及迁移均附加写保护触发器。
- 采集适配器返回时冻结接收时钟，不等后续轮询才伪造首到；NLP前绑定确切内容版本，旧正文慢结果不能覆盖新正文页面，也不能用于其催化。失败、坏hash、未知协议、未来分析/时钟、未知新版本不回退旧成功证据。
- 原文名称按当前股票字典确定性唯一匹配，并冻结名称/代码/校验方法，拒绝AI虚构代码、纯六位数字、模糊/重名/短名/嵌套名称；PIT读取不再查询当前FinanceNews或当前股票字典。校验身份不等于证明消息对该主体确有利好，研报作者/被提及主体的因果角色仍需进一步保守消歧。
- legacy_unknown不回填首到/可用时间；FinanceNews继续是可变页面投影，新增版本保护自本协议采证开始，不声称恢复了原本不存在的历史原文/分析到达证据。版本中的available_at是进程内材料已采证/分析已完成的时钟，并非数据库事务精确提交耗时凭据。
- news_evidence含所有计数贡献文章的内容/分析版本引用、各自时钟和实体验证，不只保留得分最高文章，便于复算重复消息计数。
- promotion_probability_v1要求p_raw/p_calibrated/production_probability都为有限0–1数值；缺字段、布尔、字符串、越界和未知协议拒绝，不借用旧字段。明确legacy_v0兼容只用于旧producer/旧冻结行，不伪造新协议或声称已经统计校准。
- 持久化先检验原始候选概率再过滤/建表/替换兼容记录，整批坏概率不会悄悄丢掉；新快照冻结全部三项概率及合同，兼容calibrated_probability列仍投影实际production_probability，真实p_calibrated保留在合同里。
- 模拟盘B/C/D先检查整条路线的冻结概率（包括watch/unactionable行），再做原资格/概率筛选；排序、门槛和输出用同一production值，不再SQL预过滤旧概率。坏合同拒绝当前路线且不回退旧概率/旧批次；保留T+1、实时盘口、涨跌停、风控/仓位链路。
- 模拟盘候选保留prediction_snapshot_id和冻结news_evidence（缺证为空对象），不查询当前新闻补历史、不修改订单执行授权。
- persistence返回empty_unproven/fully_filtered/invalid_candidate_identity/recorded/preserved_existing及原始/过滤/准备计数；正式调度API在prediction_health.persistence透传，明确ledger_recorded。空结果尚未生成冻结完整零候选run，不能当作成功证明。

核心新增/修改范围：backend/app/models/news.py、backend/app/news/{engine,catalyst}.py、backend/alembic/versions/029_news_evidence_versions.py、backend/app/promotion/{versioning,persistence}.py、backend/app/api/v1/{promotion,paper,tenbagger}.py、backend/app/signal/{anomaly_scanner,dragon_head}.py、相关test_news/test_promotion/test_paper测试及本进度/promotion-api文档。所有共享文件均局部改动；本轮没有编辑scheduler、settings、前端或生产配置。

### 尚未完成的后续阶段

- 完整48因子的前向不可变采证、数据上下文接入和跨雷达/预测/模拟盘复用仍须按模块推进；本批news_evidence透传只是新闻模块，不冒充全量共享因子已接通。
- 空/完全过滤批次目前只有明确诊断，没有携带独立批次时钟/上下文的不可变失败或空run；还需解决其不能遮断旧ready run的残余问题。ledger.append_prediction_run直接调用入口仍使用旧数值读取，当前正式持久化先经过已修适配器，未来应统一直写合同，不能当作所有入口已收口。
- 新闻实体角色消歧、采证字典批量性能和部署后前向可用率需继续验证。未自动运行新协议去重算/盖章旧历史。
- 运行服务、028/029迁移及防休眠/真实行情连续性仍待独立部署验收。

## 第7批（目标第11轮：正式空批次阻断与台账直写合同）

本轮仅修改 promotion/versioning.py、promotion/ledger.py、promotion/persistence.py、api/v1/promotion.py、api/v1/paper.py、新增 test_promotion_batch_attempts.py 及文档。既有 scheduler/settings/前端/研究并行改动保持原样；没有生产数据库写入、迁移、重启或订单。以下是对第6批剩余项的增量落实，不是全目标完成。

### 实际改动

- 复用 project_promotion_probability 统一兼容适配器与 append_prediction_run 直写入口。全部输入在筛选/建表/删除旧兼容行前校验；非法数不再靠 _float 变成零。合法零、独立 p_calibrated 和实际 production_probability 原样保留，旧 storage aliases 仍只投影生产输出。
- 已冻结 probability_contract 必须完整且与生产器合同一致；不覆盖冲突、畸形、未知或缺字段证据。重复经过适配器和台账保持相同合同，原始入参不变。不把旧生产器升级为新协议，也不改评分/生产门槛。
- 新增调度独立身份 ScheduleBatch，由内部生成器传入本次真实 request_started_at 与规范上下文，不依赖第一个存活候选来获得时钟。公共 GET 不获得正式落账能力；无独立身份的旧空调用仍仅返回未落账诊断。
- 对 empty_unproven、fully_filtered、invalid_candidate_identity 追加零快照、status=blocked、gate_passed=false 的不可变运行记录，元信息保留实际适配器计数和未知 universe_complete=null。部分身份缺失不再静默缩池写入部分候选；直接台账调用也拒绝缺身份。
- 空运行参考日取真实调度信号日；上游各目标候选的陈旧证据日期只记元信息，不使当天阻断落在旧日期而被消费者漏选。验证独立时钟非未来、无时区、规范上下文，目标日期无未来，原因与计数一致；不声称这些本身证明覆盖了全市场。
- 同模型/同独立尝试/同诊断幂等；后续新时钟重试追加兄弟记录，不改旧兼容行、旧快照或旧运行。内部生成器即使 touched=0 也提交已追加阻断运行。
- B/C/D维持先选各自允许时段和信号日的最新运行，再验状态；新增 prediction_batch_blocked 诊断及适配器计数。质量闸门的原始通过值不能覆盖 blocked 状态；正确后续新有效批次可以恢复读取，不回退旧概率/旧批次。
- 现有调度器以 recorded_predictions>0 判定完成并启动影子，本批阻断 touched=0，不将其误作成功或触发影子。没有修改交易阈值、T+1/涨跌停/实时盘口/风险/仓位链路。

### 验证记录

全部 pytest 使用 /usr/local/bin/python3.11，PYTHONDONTWRITEBYTECODE=1、QUOTE_ROUND_ARCHIVE_ENABLED=false、-B、-p no:cacheprovider，数据库由测试隔离；不调用实际采集/日历网络/下单。
- bash-53：原概率合同、台账、promotion API、paper概率消费者，364 passed，23.75秒，exit 0。
- bash-54：本批初始直写与空运行边界矩阵，104 passed，5.65秒，exit 0。
- bash-55：增加真实内部生成器零候选事务提交边界，105 passed，5.75秒，exit 0。
- bash-56：所有 test_promotion*.py、paper五组及factor两组联合回归，1903 passed，119.75秒，exit 0。包含研究/影子/训练材料隔离、生产概率、旧批次消费与因子缺失/IC；这是合并源码回归，不是部署效果。其后补充原因计数一致性守卫及调度零记录不启动影子的测试，最终复验另列。
- bash-57：最终源码的新增125项批次边界测试、概率合同/台账/调度延迟/promotion API/paper概率消费者六组合并复验，508 passed，30.16秒，exit 0。新增覆盖原因与计数矛盾、B/C/D上下文/信号日隔离、后续有效恢复、调度零记录不启动影子；全部本轮父会话后台任务均已收取，无遗留运行测试。
- 本轮未修改前端，未重复前端构建；以上均仅有既存 python_multipart PendingDeprecationWarning。

### 尚未完成，下一阶段必须保留的边界

1. 目前只冻结到达候选持久化适配器的空/全过滤/坏身份尝试。scheduler._build_promotion_snapshot_once 在收盘质量不ready、enforce全路由阻断、异常，以及其外层超时/过期窗口中仍可能在生成器之前返回，未追加对应失败run；旧ready消费残余在这些路径上仍需单独处理。非法概率本批明确抛错拒绝，尚未将上游计算异常包装为独立失败批次。
2. 原成功run仍沿用既有候选上下文/模型合成逻辑；阻断run用当前基础模型身份，不代表已经完成部署overlay所有组合身份与消费者查询的统一审计。适配器计数只反映它收到的候选，首板在更上游已过滤的全集分母没有被本批补造。
3. 全48因子仍只完成输入缺失合同和研究IC修复。实读 factors/base.py 看到 compute_cross_section 将相同 kwargs 传给所有股票；api/v1/factors.py 的 compute_factors 只拼30行StockDaily/FundFlow后调用 compute_single，没有逐股新闻/板块/基本面等时点上下文，也没有将结果前向冻结为共享证据。不能把48个已注册因子误报成48个已有合格实时证据。
4. factors/evaluator.py 继续明确 FactorValue 缺首次计算/可用时钟与版本，IC只作存量覆盖池研究、正式次日收盘比值，不是可成交收益。不能通过补当前时间或复制旧值伪装历史PIT合格；雷达/预测/模拟盘的全因子接入须先有前向可审计材料。
5. 新闻语义角色消歧、真实前向可用率、028/029部署迁移、防休眠及真实行情连续性运行验收继续未完成；目标保持active，不标记complete。

## 第8批（目标第12轮：调度生成前阻断与成功批次接替）

### 实际增量

- scheduler._build_promotion_snapshot_once 在确认本次交易日且实际仍在既有上下文窗口后，调用 _begin_promotion_generation，以独立数据库会话先提交 generation_pending 不可变阻断，再进入竞价采集、收盘固化检查、新闻缓存、质量审计、候选生成。不会因为持有调用者会话就提交/回滚其行情改动，也不等超时取消后才尝试补一条“失败”。
- 该记录复用上一批 append_blocked_prediction_run：status=blocked、gate_passed=false、零快照，但分母计数全为null，不能把尚未生成解释成已观察到零候选。旧证据不改写，已提交阻断可跨消费者事务继续生效；运行失败/超时/取消后仍阻止B/C/D回退各自上下文允许的旧成功批次。
- 收盘未ready、enforce全路线失败及候选异常响应保留 generation_attempt 的精确run_id/run_key及状态；不触发影子、不把零记录标完成。generation_pending表示尚未发布后续完成批次，不声称记录了崩溃后的准确失败原因；具体阶段仍看运行审计。
- 成功的正式适配器/台账也使用独立ScheduleBatch微秒时钟与批次键。旧候选展示字段仍是原秒精度，不重写原证据，但运行排序不能被其截断，避免同一秒开始阻断永远排在成功运行之后。同一精确批次幂等，不同微秒的重试不再折叠为同一运行。
- 有独立成功批次时，候选source/context/recorded_at必须与它一致（允许既有秒精度展示，不接受带时区/错秒/错误上下文），目标日期无未来；在删除兼容记录前验证，ledger直写重复防护。
- 新增边界测试先暴露影子超时路径中的日期锚定问题：初次3项失败显示completed_contexts使用date.today()，与本次尝试日期脱离。修复为固定attempt_trade_date，同时竞价健康/固化和质量审计采用同一尝试日。日历等待跨日或越窗口时不启动生成，不把昨天的请求补成今天的正式批次。
- 已提交生产成功批次后，影子超时不再追加晚于成功运行的失败标记；原开始阻断较早，成功运行可继续被B/C/D读取，已完成上下文不会被看门狗重复发布。此处仍沿用现有影子策略/门槛，未授权任何自动晋级。

### 验证

- bash-58：原批次/台账/延迟/概率合同/promotion API，439 passed，25.95秒，exit 0。
- bash-59：新屏障测试首次30 passed、3 failed；失败是影子超时时完成标记使用系统当天而非尝试日，已据实修复并复测，没有跳过测试。
- bash-60：最终34项生成屏障、125项上一批空批次及19项调度延迟三组合并，178 passed，15.30秒，exit 0。
- bash-61：扩大回归时引用了不存在的test_quote_round_integrity.py，pytest在收集前exit 4/no tests ran；通过glob核实真实test_quote_round*.py四组路径后重新启动联合回归，不把该失败计作验收。
- bash-62：扩大合并回归1997 passed、2 failed；一项旧daily_consumer断言仍要求run时钟截断到整秒，已改为严格断言生产器微秒时钟及批次键（保留其它研究隔离断言）；另一项新超时测试100ms预算在屏障提交前就耗尽，未进入其声明要测的候选工作阶段。新夹具给提交阶段足够预算并显式断言已进入候选，生产超时配置未修改，提交前失败的限制继续如实保留。
- bash-63：新增真实内部生成器正路径测试首次因夹具get_trade_session不接收可选时间参数失败，已修正夹具签名；生产生成器/注解/持久化仍不替换成mock。
- bash-64：新生成器35项及daily_consumer43项合并，77 passed、1 failed；旧时钟断言首次edit未命中，运行收集到的仍为旧断言。已read后准确定位修正，不修改任何评分/研究隔离断言；最终整组重跑另列。
- bash-65：冻结最终源码后，全部test_promotion*.py、paper五组、scheduler两组、quote_round四组及factor两组联合回归，**2000 passed，2条既有依赖/concat警告，140.21秒，exit 0**。覆盖真实内部生成器→候选注解→正式持久化→微秒批次排序（外部材料为隔离夹具），以及研究不改生产/影子隔离、行情轮次、因子输入和IC。父会话本轮全部后台任务已收取，无遗留运行测试。
- 所有测试均在pytest临时数据库中，时钟/日历/采集/质量源/候选和影子等待为隔离夹具，不运行真实采集或订单。未修改前端，也未运行部署迁移/重启。

### 总目标仍未全部完成

1. 第7批“生成器之前失败无run”的缺口，现在覆盖**成功提交开始阻断之后**的质量失败、计算异常、超时、取消；并非覆盖所有宿主故障。交易日历确认之前失败、从未进入有效窗口、SQLite争用/持久化事务失败或进程在提交前退出仍没有持久化阻断。源码会拒绝继续生成，但不能谎称旧消费者必然已有新run可读。调用者若已持有SQLite写锁，独立阻断事务仍可能等待到外层超时；当前实际调度调用不传调用者事务。
2. 未完成完整48因子逐股/时点上下文、前向不可变采证和跨雷达/晋级/模拟盘接入。既有FactorValue历史不能补时间盖章，真实IC仍研究用途，不能据此调权或晋级。
3. 新闻实体语义角色消歧、部署模型overlay组合身份与消费者查询的全面审计、028/029迁移及部署后的持续行情/可用率验收仍需继续。各次测试通过不等于服务已加载新源码。
4. 本轮涉及共享scheduler.py的局部新增方法/调用、promotion/{ledger,persistence}.py、test_promotion_batch_attempts.py、test_promotion_daily_consumer.py、新增test_promotion_generation_barrier.py及两份文档；保留其他并行改动，未改settings/前端/阈值、未下单、未改历史行情或预测证据。
5. 已达到本目标当前12轮上限，保留goal active，不虚报全目标完成或权限阻断；剩余全因子前向采证/接入与运行部署验收需要后续继续。

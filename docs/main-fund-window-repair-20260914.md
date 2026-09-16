# 9/14 第三轮：主力资金五会话窗口修复

## 任务、时点与边界

- 本轮为9/14盘后复盘建议的工程修复，不重新生成9/14历史复盘或预测。
- 已重读 AGENTS.md、.workbuddy/memory/MEMORY.md 资金/日期说明及相关评分消费者。
- 修改输入窗口可能改变排行、候选及预案的资金完整性降级；不修改 BullScore/Tenbagger/NextDayPlan 的评分公式、权重、买入阈值，不训练、不晋级Champion、不交易。
- 无生产业务GET/数据库读写、订单、迁移、部署；不改历史DB、旧预测/行情/复盘证据。隔离pytest使用临时库，HTTP禁止；浏览器API全部fixture拦截，WebSocket关闭。
- 未改scheduler（父侧正式窗口恢复）、paper/预测诊断（其他代理）、models、migrations、main、公共前端api/globalstyles。

## 核验到的实际缺陷

1. bull排行把7自然日范围sum叫5d；tenbagger排行用10自然日sum。
2. 低位形态与批量明日预案也使用7自然日资金和。
3. 个股详情limit5只按有数据的行取数，缺某个会话会跨越缺口补更老日；无来源/无时钟、部分样本和也会发布为5d_total。
4. _load_recent_main_fund_map用7自然日+每股取5条，并用or0把缺失当真实0；其下游detector本身仍有None→0，因此不能只把这条loader的0改None后直接传入。
5. 行情与资金日期、窗口成熟度必须分开：当天盘中累计不是完整日资金；不能读取到15:00之后再把收盘资金装进14:59排名时点。

## 修复契约

### 五个已确认收盘会话

新增 backend/app/data/main_fund_window.py：

- 复用现有 data/index_history.expected_index_trade_days；该helper仅读 TradeCalendarModel，并用官方休市覆盖/周末复核。应开市工作日缺日历则fail closed，不用周末推算开市、不调用会在线抓取/补库的trade_calendar方法。
- 指定through_date与decision_at；当天15:00前排除当日，15:00起可纳入当日，但仍须该日真实收盘资金证据已可见。周末/长假从已确认会话向前取，恰好5个，不是7/10自然日。
- 以source_quote_at >= 15:00作为完整日资金的必要条件。只拿14:59或更早最后一条时不能声称完整收盘资金；不会延长新鲜度阈值以“恢复覆盖”。
- 逐日验来源版本、金额及占比有限值（bool/None/NaN/Inf不合格）、原源/接收/观测三时钟与严格顺序、可见性；沿用fund_clock_status(require_live=False)验证原采集时新鲜度，不要求过去会话在今天仍是实时数据。
- 一日缺失/不合格/未来/非完整收盘，整个5会话total=None。不会返回部分和或跳到更早行凑5条。真实五日零和仍是0。总和溢出也未知。
- 返回valid_count、逐日status/history、session_dates、cutoff、through_date、complete和版本five_confirmed_closed_sessions_v1。
- 明确basis=dated_latest_not_pit：FundFlow是每股每日最新行，不是不可变历史PIT账本；晚于cutoff的latest行拒绝，不倒推旧值，不复制当日latest到旧日。现在可见的过往日完整行只称dated研究证据。

### 消费集成

backend/app/api/v1/tenbagger.py局部：

- 两种rank、低位形态、批量明日预案、个股详情、API异动recent loader统一共享上述窗口，所有7/10自然日fund sum已移除。
- rank及批量plan在开始计算时冻结资金decision_at，跨15:00计算不移动窗口；缓存返回原始窗口时点，不把缓存当新观察。
- rank传入模型的5会话金额保留None，不在loader层用0伪造。模型内部既有None中性数值防御保持原样，外部另给fund_5d_complete/status/window；不能把模型内部中性值解读为测得资金0。
- 批量plan/个股plan复用既有fund_5d_complete门控；缺失传False，不改引擎买入阈值。对外5日额None且方向“数据不足”。
- recent loader只给下游完整合格五会话列表；任一缺失就给空列表（显式字典，避免None触发下游旧DB fallback），不让下游把单日None当0。
- 内部详情start/end仍保持date类型，外部JSON日期/窗口用ISO字符串。
- 缓存版本：rank v5→v6、plan v57→v58（兼容列表只v58）、anomaly v18_fund_signal_v1→v19_fund_window_v1。旧快照不删除/不改写，不作为新窗口版本返回。

### 局部UI与CSV

frontend/src/views/tenbagger/components/RankTabContent.vue：

- 保持上轮当日资金snapshot_known提示，新增“近5会话”行。
- 完整真实0显示0.00亿；不完整显示“未知”；完整负值保留符号。
- title列出确切5会话首尾日期、有效n/5、原判定时点、窗口版本，并说明“每股每日最新记录，不是历史PIT还原”。
- 两种排行CSV/Excel列补窗口金额、完整状态、日期证据。未改公共样式/API、其他页面。
- frontend/e2e/tenbagger.fund-display.spec.cjs仅增加对应隔离交互测试：长假日期、零/负/缺失、两种排行模式切换和CSV下载，无业务请求透传。

## 精确文件

- backend/app/data/main_fund_window.py（新增）
- backend/app/api/v1/tenbagger.py（资金窗口/快照局部）
- backend/tests/test_main_fund_window_20260914.py（新增）
- frontend/src/views/tenbagger/components/RankTabContent.vue（资金行/提示/导出局部）
- frontend/e2e/tenbagger.fund-display.spec.cjs（对应隔离测试）
- docs/main-fund-window-repair-20260914.md（本文）

本轮没有修改 main_fund.py，也未回改上轮文档。

### 并行验收产物与源码冻结

- 按父侧协调，最后一轮Playwright显式指定 `--output=../outputs/repair_validation_20260914_round3/funds`；本次新增截图用testInfo.outputPath，不再写共享frontend/test-results路径。此前默认Playwright可能清理共享test-results，未手动删除其他任务产物；不尝试伪造已消失的其他分支证据。
- 本分支独立统计，不纳入父侧正式恢复717项联合测试结果。
- 资金源码已冻结，后续除本文最终验证记录外不继续修改。SHA-256：
  - main_fund_window.py: `ecdeba40bdb6cffe59ad1d7868cc5474b93fd1af119939762ea5fea022813513`
  - tenbagger.py: `564dc837eac12314664723c9783d33369cf95524ffbfb3ceb42545e87aad76fe`
  - test_main_fund_window_20260914.py: `507b94b23573106f0bdc6aef3af8ae1c8905888a980708867ee0822f6d69a942`
  - RankTabContent.vue: `77b80015d59aff3f1c89461ba32d599f4f26506c58394f7930fb3bb83cddb136`
  - tenbagger.fund-display.spec.cjs: `2f251fd649464c04beb9d6820b291a7169a1e09424386fbec4fa0aa0b830706c`

## 测试与验证进度

已完成：

- 首轮窗口32例+上轮消费者22例：54 passed（8.17s）；之后新增真实零与缺失分别传入既有plan门禁的第33例。
- 窗口/消费者/主力投影/异动执行/缓存5文件：131 passed（10.71s）。
- 最终窗口33例+原NextDayPlan引擎7例：40 passed（8.86s），未修改引擎文件。
- 最终frontend build成功（7.39s），仅已有大chunk提示。
- 源码冻结后独立6文件资金回归：139 passed（8.94s），包含窗口/上轮消费者/投影/异动执行/缓存/预案引擎。
- 最终隔离Playwright两例通过（4.2s）：本次五会话/CSV + 既有rank资金时点；运行在既有127.0.0.1:5173 Vite，仅静态资源实读，API/WS拦截，无生产GET。独立截图：outputs/repair_validation_20260914_round3/funds/e2e-tenbagger.fund-display-288b3-daries-zero-missing-and-CSV/main-fund-window-20260914-isolated.png。
- 首次新e2e拦截通配过宽误拦/src/api/index.js导致空页；已收窄为pathname.startsWith('/api/')，复测通过。不是产品资金错误。

适配前扩展8文件：175 passed / 7 failed（12.24s）；以下为保留的初始失败记录。父会话随后明确授权3个相关旧测试局部适配，处理与最终结果见文末第四轮补充：

1. test_fund_downstream_contract.py四个参数例仍要求“无日历单行/无时钟行=5d_total 2亿”，新契约必须None。
2. 同文件一例在日历缺失时仍期望从原始行计clock_unknown_count=1；新窗口未建立、不读取资金，count=0，并应断言calendar_incomplete。
3. test_stock_next_day_plan_detail.py一例未种日历/来源/时钟，仍将5条无证据行当完整日；应补已确认日历和15:00三时钟fixture，保留原始行不改写断言。
4. test_tencent_fund_consumer_labels.py fake DB仅允许每SELECT显式autoflush=False，不兼容被复用calendar helper通过db.no_autoflush上下文保护读；应在该fake fixture识别calendar查询，或mock expected_index_trade_days返回[]以保持测试专注当前资金。

未隐瞒旧测试失败或放宽生产规则迁就旧fixture。新测试额外覆盖长假、盘中/15:00边界、日历缺口、缺日、零/负数、非有限、未来覆盖、无自动flush、原模型3亿/-2亿阈值等号边界及缓存保留原时点。

所有pytest命令使用：
`PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false /usr/local/bin/python3.11 -B -m pytest ... -q -p no:cacheprovider`。

## 父集成与仍不能证实

- **本分支交接时的范围外消费**：scanner默认历史/单股路径与连续流向曾待集成；父侧已随后接管这些修复，最新同步见文末。仍有scanner龙头装配两处及resonance一处latest资金无时钟fallback待处理，不能声称全资金消费闭环完成。
- 无生产日历、历史资金收盘时钟或版本部署验收；不声称9/14已有完整5日资金覆盖，也不伪造历史PIT证据。来源修复晚于历史数据时，历史缺证据保持未知可能显著降低覆盖。
- 未测收益/收益改善、Champion效果或实盘交易；变更是数据输入纠错，可能改变排名并使原来误放行的预案因资金不完整而降级。
- 既有Bull/Tenbagger模型内部对None的数值防御未改，不是新风险容差；对外窗口质量必须一起读。
- 5会话资金与另一条5日价格涨跌窗口是不同契约，本轮未改价格/K线收益窗口、价格容差或其他代理price_chain。

## 第四轮：旧测试证据契约适配（授权后）

仅修改以下3个既有测试与本文；上述5个源/测试文件SHA-256再次核验完全一致，生产窗口、评分、阈值和前端保持冻结，父侧负责scanner直接消费者。

- backend/tests/test_fund_downstream_contract.py
  - 原单行当前资金场景保留current金额、源时钟、stale/future拒绝及原行不改写检查；明确无日历不能形成5会话窗口，total=None/complete=False/status=calendar_incomplete。
  - 新增2个独立日历场景：五个明确收盘会话中含真实零和负额，合计-2亿元；当前资金stale不影响已验证的历史收盘窗口。破坏其中一天源时钟则4/5、whole total=None、unknown clock=1，原始负额/缺时钟不被改写。
- backend/tests/test_stock_next_day_plan_detail.py
  - 正常路径补独立交易日历和每天15:00、15:00:01、15:00:02合法来源/接收/观测时钟。
  - 固定as_of=2026-09-01 10:00，保留5会话20亿元及8/25–8/31边界、剔除第6条旧日、当天无时钟资金不得成为current、原DB行保持不变断言。
- backend/tests/test_tencent_fund_consumer_labels.py
  - 正常主力标签测试改用真实临时AsyncSession，独立种5个确认会话及每日日终三时钟/零值；保留腾讯源版本、单位、细分None、当前真实零、三时钟、alias标签断言。
  - 额外pending ORM行证明当前/日历/历史读取均不会autoflush；不再用mock的单一statement flag误判真实no_autoflush上下文。缺证据/未来/伪造快照等其他测试不变。

第四轮测试文件SHA-256：
- test_fund_downstream_contract.py: `9b4c7177a16ba39c41c9ba682fbd4cf27e1f3079c1e0be5146efe4009fccee25`
- test_stock_next_day_plan_detail.py: `52ee278ef29c68fb18ab3730afe645c08a629512c93233bdd675880c6ee3c540`
- test_tencent_fund_consumer_labels.py: `6fb9dd93358cc7a6f2598e63b47c3da32402e08f3d8c501f31699dda62e384c1`

本轮不运行前端构建/截图（前端源码未改，沿用第三轮独立build/E2E证据）；如需追加只使用outputs/repair_validation_20260914_round4/funds，不清理任何共享test-results。

最终结果：
- 33个窗口边界例 + 3个适配文件：86 passed（11.96s）。
- 全部 `tests/test_*fund*.py` + `tests/test_stock_next_day_plan_detail.py` + `tests/test_next_day_plan_engine.py`：**660 passed（33.39s）**，退出码0，只有既有python_multipart弃用提示。
- 完整命令：`PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false /usr/local/bin/python3.11 -B -m pytest tests/test_*fund*.py tests/test_stock_next_day_plan_detail.py tests/test_next_day_plan_engine.py -q -p no:cacheprovider`。
- 原7个旧fixture失败已消除，保留旧契约基线说明；没有以放宽生产规则使测试转绿。未把独立660项与父717项相加或宣称互不重叠。
- 本分支测试与生产源码已冻结；本轮确切变化为3个测试、本文和round4/funds验证摘要。父侧后续接管修改不受本分支冻结限制，整文件哈希只代表当时基线。

## 父侧后续集成同步（非本分支重新验证）

- 已读取当前tenbagger.py确认实际ANOMALY_SNAPSHOT_VERSION为 **v20_fund_window_streak_v1**；前文v19是本分支交接版本，不是最新运行源码版本。
- 据父侧报告：scanner默认历史/单股路径已统一严格5会话window，_scan_anomaly_snapshot冻结同cutoff交给scanner，非空recent_funds_map仅作hint并重读权威窗口，{}仍明确不可用不查询；资本event附fund_5d_payload。
- 据父侧报告：capital_anomaly连续天数由“5天内同号计数”修正为“从最近已完成日开始，遇0或反向即截断的连续run”，要求5个完整会话及有限值，门槛、分数、其它模型权重不变，增加专测；v20阻止旧连续口径缓存复用。
- 父第一批156 passed；后续8文件联合仍在运行。此处为父侧进度，不与本分支660项合并，不把660当作v20最新集成验收。
- **仍未闭环**：scanner龙头装配两处、resonance一处latest资金无时钟fallback尚待父后续处理；生产资金覆盖、历史PIT真实性和收益效果仍未证实。
- 本次只同步本文，无任何源码或测试修改。

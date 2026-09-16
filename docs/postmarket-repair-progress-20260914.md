# 9/14 全面复盘后修复进度（新任务）

## 边界

用户已授权修复全部建议。旧目录 `outputs/postmarket_review_20260914_1717/` 为冻结证据，不改写其数据库、结论和清单。不下单、不放宽生产阈值、不自动调权/晋级、不以参考收益代替可成交收益。保护工作树并行改动，逐文件定点修改。

新目标：`goal-da398089-e58d-49ed-9d8d-e59e9ecce4c4`。旧复盘完成不代表新修复完成。

## 逐项验收清单

|模块|已有源码/事实|仍需验收|
|---|---|---|
|行情/休眠|原有intercommit连续性、A2有界FIFO、PID绑定AC caffeinate；先前pmset证据说明两次长断档来自休眠|本轮补尾部断流/调度漏跑观察；部署与下一交易日连续采证。合盖/手动休眠须保持唤醒或常开主机，不能靠调大90秒阈值解决|
|正式预测|已有独立提交generation_pending屏障、严格概率、失败不回退旧榜；ScheduleBatch是值对象而不是表|逐路线身份/门禁覆盖；进程缺席、日历/屏障提交前失败的缺批诊断；部署|
|真实资金|当前scheduler已读取FundFlow并经过current_main_fund_evidence，不直接读取StockSpot；末态0覆盖不能简单判源码仍错用字段|缺失/过期/零值语义及各消费者一致性；代理定点修复。不用盘后累计值回填早盘|
|身份/K线|存在两只池内缺数据、退市标签冲突、混源价格链断点|共享质量掩码/隔离原因覆盖各模块；不覆盖旧历史|
|新闻|已有内容/分析版本；标题漏主体、交易标的/关联方/否认归因待修|代理补保守角色证据与边界测试；仅新版本，不回填旧报道|
|共享因子|48因子严格缺失值与真实IC已有源码；历史FactorValue非PIT|前向原料/版本冻结、延续/回收/午后启动分路线研究、雷达/预测/模拟盘复用|
|漏斗/退出|复盘分清确认/发送/委托/成交与旧版拒绝/盘口不可成交；position_bound_exit已有源码|逐阶段不可变原因、旧版退出回归、轮次/物理时钟分离、经济损益与费用显示|
|隔离评估|复盘已有正负反例，不等于策略验收|同预算容量/退出对照、时间外校验与成熟T+1；不挑赢家调参|
|部署|服务15:34启动，不能把新代码套回盘中|迁移预检、回归、安全部署、原URL核验及前向采证；源码测试≠部署/盈利|

## 本轮运行核实（约18:18–18:30，Asia/Shanghai）

- 实际目录 `/Users/youzix/WorkBuddy/Claw`。
- 只读 `http://127.0.0.1:8000/health`：PID57866仍为2026-09-14 15:34:23启动，scheduler running=true、49 jobs，pipeline为intraday_pipeline_v2_nonblocking_news；无本轮operational_health字段。
- caffeinate requested/process_running=true，但lid_sleep_protected=false、manual_sleep_protected=false。不声明解决合盖休眠。
- 生产 `backend/claw.db` 以SQLite URI mode=ro + query_only读取：Alembic仍027_fund_order_breakdown；028评估表和029新闻表已存在，但两个news版本表没有SQLite append-only triggers。create_all不等于迁移完成；029可补触发器，待迁移验收。本轮未执行生产迁移。
- 当天promotion_prediction_run按created_at查询：0925=1、0935=2、1000=3、1030=3、1305=2，均completed；仍无1510。completed不等于逐路线gate_passed，不补造15:10预测。最初试查不存在的trade_date失败，查ORM后改用created_at（锚点列为reference_trade_date）。
- 当前news_content_version=512、news_analysis_version=212、factor_evaluation_run=0；分析版本增加来自运行进程，不修改旧冻结复盘的47条口径。

## 本轮实施：轻量连续性/调度观察

文件：`backend/app/data/pipeline_runtime.py`、`backend/app/data/scheduler.py`（定点接入）、`backend/tests/test_pipeline_runtime_health.py`。

1. `/health.pipeline.operational_health`区分存活、当前新鲜、日内已见缺口和业务批次；scope=current_process_only，全天完整性固定not_certified。
2. 复用原交易时段/90秒阈值；午休休市不误算。没有后继成功commit仍可发现尾部stale，新鲜一轮不抹掉当天缺口。
3. 记录missed/max_instances/error，以及Python正常返回但业务status失败/降级；后续成功不擦除当天失败，不把正常执行当正式预测成功。
4. 只在DB成功commit后观察心跳；visible_at/commit_to_visibility_sec区分原committed_at（位于提交前）与真实可见钟，不改旧记录。
5. 独立15秒观察协程只读内存，不访问DB/网络/交易，状态变化记日志；stop撤销协程和监听器，记录有界。
6. 未修改下单、T+1、价格/盘口、容量、风控或晋级逻辑。

验证：首批64 passed、1个既有python_multipart警告、2.90s。覆盖306秒断档、90/91秒边界、午休节假日、缺首次轮次、未知日历、重启边界、坏时钟/时区、返回失败、撤销/限长。扩展测试首次命令引用了不存在的test_paper_momentum_quote_inbox.py，pytest未运行（exit 4）；定位真实测试文件后重跑。未将该次错误计为通过。

## 本轮新增：禁止以新昨收覆盖旧日K

发现 `_spot_to_kline_fill` 原逻辑会在0.1%–12%差异范围内，把旧日K close/high/low/change_pct直接改为新快照推断值；旧测试还要求该行为。现改为读取旧值、不写旧行，返回/健康页记录价格链conflict或unknown，保留两个来源数值，不猜测是复权还是数据错误。超过12%的差异也明确标记，不静默忽略。

新增 `backend/app/data/price_chain.py`，将既有0.011元容差和正式来源集合抽成共享常量；`strategy_iteration_shadow.py`、`signal_research.py`、`promotion/outcome_evidence.py`保留兼容别名，收益/执行阈值不变。采集返回price_chain_quality；健康页只保留最多50条示例及全量计数，明确previous_stored_bar_date不等于已证连续交易日。

注意：这只移除“新昨收改旧K”路径，尚未宣称所有K线回补/删除路径都已审完。下一轮还需审查非交易日/未来spot_fallback清理、终场重建、夜间THS回补与身份隔离的边界。

扩展调度/正式预测屏障/下一轮撮合/持仓风控与inbox相关验证：127 passed，1 warning，23.60s。新增价格链、完整scheduler_kline_fill、runtime、真实IC、A2影子测试：165 passed，1 warning，12.90s。两组有重叠，不相加冒充独立用例数。所有pytest使用隔离临时库、关闭行情归档，未调用生产写业务。

`git diff --check -- backend/app/data/scheduler.py` 返回exit 2：工作树相对Git基线有12处尾空格（包含本轮编辑前读取即存在的start与日K方法空行、旧解析器行）。不是pytest失败；未为清理这些既有格式覆盖并行代码。

后台代理：资金 `643381da-2d0f-40f2-84db-1eabdeeb836a`；新闻角色 `ff74cf15-f332-418c-b5ec-faa93d4b7e52`。父会话此阶段启动的bash-72、75、80、83均已收取完成；75是已说明的错误测试路径，其余通过。

## 本阶段最终联合回归

bash-84已收取：11个相关测试文件联合运行 **265 passed，1 warning，32.68s**。涵盖新运行监测/价格链、原调度与正式预测开始屏障、下一轮撮合、持仓风控/A2收件箱、日K补全、真实IC及A2影子。警告为既有python_multipart待弃用提示。此结果不包含尚未收取的资金/新闻代理验收，也不代表部署或全部目标完成。

## 第一阶段交接时尚未完成

当时资金/新闻代理结果尚待父会话核验，未部署、未生产迁移、未改前端。后续状态以以下续轮记录为准。

## 自动续轮1：资金/新闻集成与前端合同（9/14约18:31–18:46）

### 代理产物核验

已读 `docs/main-fund-consumer-repair-20260914.md` 与 `docs/news-role-repair-20260914.md`，阅读核心实现并运行父会话联合回归。代理当前均ready，本轮未再次委派业务执行。

- 资金：`main_fund.py`统一拒绝原因和ISO时钟输出；`api/v1/spot.py`同次查询返回missing/stale/future等，不显示被拒金额；`api/v1/tenbagger.py`排行缺失值改null，增加snapshot_known与源时点，用v5缓存身份隔离旧missing-as-zero快照，不改评分参数。prewarm分母明确是已观察资金行，而非全市场覆盖。
- 新闻：新增`news/roles.py`原文角色证据；标题主体/交易标的与正向受益分开。拟议交易、关联主体、否认/风险、收入受限、涨后报道和多主体方向不明保守降级，不把模型乐观摘要当事实。新实体映射仅在新分析available_at后生效，原内容/分析及旧预测不修改。仍是保守规则而非完整语义模型，复杂事件可能漏拦或过度拦截。
- 父会话额外修复`news/catalyst.py`：先完整验证新角色载荷，再一次性发布分析字段；畸形role.entities不再在异常路径泄漏已复制的利好/板块。新增3个边界测试；超大confidence的OverflowError亦保守拒绝。

### 父会话资金集成

- `data/scheduler.py`：没有合格资金时写SQL NULL，不再补0；真实零仍为0。version升为breadth_index_quality_v4_nullable_funds，只标新快照。
- 不改变原None→质量degraded/禁止买入链路，也不改80亿评分边界或资金新鲜度阈值。
- 同一原查询/同一cutoff统计合格、缺失、stale/invalid等；`/health.pipeline.sentiment_funds`明确qualified_tradeable_rows_only、合格/分母/观测行数以及snapshot_persisted。失败返回failed，防止被调度执行成功掩盖。
- `risk/circuit_breaker.py`、`api/v1/sentiment.py`保留资金None到对外返回，不再把未知展示为实测0；不改变既有仓位规则。
- 原资金迁移测试的目的为026→027，却upgrade head并写死027。固定测试升级目标为027，不把当前029头误判为资金逻辑失败。
- 新增`tests/test_evidence_migration_preflight.py`：在临时库模拟027已有内容/分析/IC表但无新闻触发器，真实CLI升029两遍，核对原行字节不变及UPDATE/DELETE被拒。此为部署预检，**未执行生产迁移**。

### 前端与运行核验

- `frontend/src/views/tenbagger/components/RankTabContent.vue`：明确排行生成时资金/源时点/未知，提示不是当前交易资金确认；CSV/Excel增加快照状态、时点、用途。零值仍显示0.00亿。
- `frontend/src/views/stocks/Detail.vue`：明确本次查询合格、过期、时钟异常、来源待核或未知；去掉两处把历史主力资金硬称东财的标签。
- `npm run build`成功，7.25s；仅现有vendor分块>500KB警告。
- 复用原5173，PID21381 cwd确认为本项目frontend，没有另起替代服务器；所有业务API及WebSocket隔离。Playwright最终 **17 passed，11.9s**，覆盖排行两模式、零/未知、详情过期/未来、过滤/导出与390px横向可用性。
- 第一轮E2E 13通过4失败为新增测试使用**/api/**误拦截Vite /src/api/index.js，修正为**/api/v1/**后全通过；不是通过放行真实业务请求解决。
- 18:44左右只读8000 health：operational_health_loaded=false、sentiment_funds_loaded=false，仍旧后端。前端源码/构建验收不代表后端修复已上线。

### 本轮测试记录

- 首次后端386通过1失败：stale fixture实际上把旧source与新received/observed混合，按现行规则正确判invalid；改成三钟均旧且顺序有效，不改阈值。
- 第二次386通过1失败：冻结datetime子类影响isinstance(DB返回的普通datetime)，导致宽度样本被测试环境误排除；修正测试时钟类型判断后387 passed（11.58s）。
- 加畸形角色原子拒绝后：**390 passed，1 warning，12.16s**。
- 最终扩展联合回归（全部16个资金测试文件、情绪、新闻与迁移预检）：**866 passed，1 warning，30.36s**；bash-92 exit 0已收取。警告为既有python_multipart弃用提示。
- 父会话本轮bash-85至92均已收取完成：85/87为已解释的测试时钟失败，88为隔离路由误拦截失败；86构建成功，89/91/92后端通过，90前端17项通过。无本轮遗留后台命令。

### 下一步仍需完成

全目标保持active，未下单、未放宽阈值、未回填历史或晋级Champion。待完成：正式预测缺批/逐路线消费覆盖、证券身份与全部历史K回补路径审计、共享因子前向原料及三模块统一事件漏斗、经济损益/物理时钟、容量/退出时间外研究、生产迁移与安全部署及下一交易日前向验收。另已发现tenbagger最近10个自然日汇总被称作5d及历史资金缺值转零，需独立修复窗口契约；不能在本輪语义修复中顺手改变评分结果而不验收。

## 自动轮次2：K线追加隔离 + 正式批次诊断 + 经济损益展示

### 父侧K线修复
- 完整报告：`docs/kline-evidence-repair-20260914.md`。
- 新增`app/data/kline_observations.py`、`StockKlineObservation`、030迁移：保存原投影和新候选，禁止修改观测；历史缺口/差异只进未审核版本，available_at=NULL，不自动用于PIT/策略/标签。
- scheduler三条日K写入统一接入，只有实际当日+调用方交易日历确认才允许投影；腾讯终场保留原值后固化，THS/fallback不能降级已有终场。
- 去掉所有scheduler StockKline DELETE；通用StockKline upsert明确拒绝。代码维护回补CLI仅追加候选，旧派生字段覆盖CLI改为真正只读预览，--apply拒绝。
- THS首根未知昨收/涨幅及空换手保留NULL；有限数/OHLC包络/量额边界隔离。收盘健康只认已知正式来源和有效OHLC，陈旧与“新鲜但坏OHLC”分开统计，不删坏行改善分母。
- 030真实CLI临时库预检覆盖新表和已建表缺触发器、重复升级、原价格/原证据不变；没有生产迁移。
- 父测试先49 passed/4 failed（旧断言要求删除/回填，已按新保留契约调整且补充隔离证据断言），再81 passed、310 passed，有限OHLC扩展316 passed（45.00s）。它们重叠，不能累加。

### 并行正式预测诊断
- `docs/promotion-batch-diagnostics-repair-20260914.md`；新增纯诊断/SELECT-only loader及专用GET `/api/v1/promotion/batch-health`。
- 从不可变run/snapshot对照原正式窗口，明确not_due、窗口仍开、缺持久化尝试、未完成屏障、批门/逐路线blocked/unknown/错身份，不借旧成功掩盖新失败；不推理、不补旧run、不改门禁。
- 代理18:57 mode=ro/query_only核查11个9/14真实run；1510仍缺批，2000未到时点。诊断不能证明15:10进程缺席的具体根因。
- 代理原联合503 passed；父复查要求补nullable/未知route与目标日期畸形边界，作为后续小补丁单独验收。

### 并行模拟盘经济展示
- `docs/paper-accounting-repair-20260914.md`；新增account-local Decimal库存/费用重放，不改旧账本、账户现金、成交或风控。
- 新API字段/页面分列gross浮盈、余仓已付买费、净浮盈、持有期净实现、今日NAV估算、旧买费桥接与完整轮次；不预扣未来卖费、不混账户/协议/版本样本。
- 13账户冻结证据只读复核：gross1343.99、余仓买费45、net1298.99、旧买费桥接合计-141.97，现金/资产残差均0；这不是收益改进承诺，冻结SHA未变。
- 代理352后端/1冻结事实测试及11前端通过；父复查要求把cash/asset residual不平衡原因明确显示，避免UI误称缺数据，后续小补丁单独验收。
- 原paper GET的get/create/refresh仍可能写，新增loader只读并不等于原整个endpoint只读；未调用真实业务GET验收。

### 父交叉验收 / 部署事实
- 新联合后端（25个测试文件：K线、预测批次/门禁、模拟会计/风险、IC、冻结样本）：**1162 passed、1 warning、96.63s**，bash-111 exit0已收取；包含冻结库哈希前后不变断言。代理后续复查边界小补丁不借这次基线结果声称已全部验收。
- 原5173隔离前端联合：build成功7.13s，Playwright **28 passed / 20.7s**（bash-112，三个文件、业务API全部fulfil、WS关闭、390/1440）。父实际查看390截图，分项可读、无横向页面溢出；样本不是生产数据。
- 19:08:32 /health只读核验：旧PID57866，operational_health、sentiment_funds、kline_observations均未加载；真实库仍027、无stock_kline_observation表。5173仍原PID21381，无新服务器。**本轮未部署、未迁移、未下单、未写生产历史。**
- AST校验本轮7个父Python源码/迁移/维护工具通过；paper相关tracked diff --check通过。scheduler仍保护既有大幅并行改动，不把整文件diff归为本轮。
- 父本轮后台命令98/105/106/109/111/112已全部收取；98旧删除/回填断言失败已修，余者通过。两代理仍负责收尾边界补丁，下一轮按各自实际最新产物/测试接续，不重做其运行中工作。

### 下轮优先队列（目标仍active）
1. 收齐两代理的上述边界补丁/最新测试，接入最终交叉结果。
2. 正式窗口安全重试/启动缺席观测与逐路线消费者：只能真实时点新增仍合法窗口尝试，绝不补9/14已过期1510。
3. 证券身份/ST/退市/20%板一致性、近期回补8.8%采证筛选及所有K线消费者质量合同；当前追加候选尚未获准正式消费。
4. 修复排行“10自然日叫5d”和历史资金缺值转零的窗口合同，不未经隔离评估改变生产评分。
5. 共享因子前向输入/不可变事件漏斗、物理订单时钟、容量/退出时间外对照；部分减仓再加仓的未来费用分摊另做守恒回归，不改旧成交。
6. 028/029/030受控迁移、源码部署与真实下一交易日前向证据分别验收；当天新买入T+1尚未到可验证退出窗口。

## 自动轮次3：盘后正式窗口恢复与收尾边界

### 新修复
- 完整说明：`docs/promotion-window-recovery-repair-20260914.md`。
- 父仅局部修改scheduler、settings及新`test_promotion_window_recovery.py`。补上原守护未覆盖的15:10/20:00，从名义时点到原窗口末分钟内恢复；原五个盘中恢复时段不变，不提前、不扩窗、不补过期9/14 1510。
- 复用原生成路径的单owner/冷却/120秒超时、generation_pending提交、日历/收盘/新闻/概率/逐路线门禁。不改模型、权重或买入条件。
- 盘后新增限时只读probe，读取不可变run/snapshot恢复已完成标记；completed但route blocked仍保留，不靠重复生成挑有利批次。最新pending/失败不能被旧成功遮盖；未知/坏证据不自动修历史。
- 增加生成epoch，防Cron在SELECT期间开始且失败后，被旧探测结果重新置为已完成；stop在APScheduler已停时也取消恢复任务。恢复结果有实际checked_at/context/attempt_status与明确current-process scope。
- 新配置PROMOTION_RECOVERY_PROBE_TIMEOUT_SEC默认3秒、有限正数上限30秒，仅控制日历/台账只读探测预算。

### 上轮代理收尾已复核
- 预测diagnostics原48测试另加24例，nullable/空/未知route、坏targetdate显式invalid/unknown且保留分母；代理527 passed。本轮父联合已覆盖该最新文件。
- paper residual补丁已冻结：现金/资产残差不平衡分别给出真实原因，主/挑战者页面可见，null/0分开且不重复扣净值。代理18后端/4E2E及build通过；父已读helper与页面局部并复跑。
- 仍未触碰物理订单时钟、未来部分减仓/再加仓费用入账、原paper GET写刷新；不将新增loader只读误称整个接口只读。

### 验证
- 父后端：118 passed（117）、211 passed（118）、216 passed（123），均为递进重叠集合。
- 14文件联合（恢复、真实只读ledger探测、完整开始屏障、预测概率、K线、风险、会计、迁移等）：**717 passed、1 warning、73.17s**，bash-126已收取。
- 最后一处恢复状态展示fallback改进，单文件37测试：**37 passed、1 warning、3.99s**（130）；不与717相加。
- paper两文件前端 **12 passed / 11.5s**、build **7.25s**（119）。默认共享test-results随后不再含本轮截图目录，未假称截图仍在；通知资金代理避免清理共享目录，改在独立`outputs/repair_validation_20260914_round3/paper/`重新验收4项（131，4.8s），父实际查看390截图，分项正常换行，无横向页面溢出。全程API/WS隔离，截图是fixture。
- 父117/118/119/123/126/130/131全部完成已收取。warning仍为既有python_multipart、Vite vendor大chunk和Playwright色彩环境提示。

### 仍未部署 / 下轮工作
- 19:25:08真实8000 health仍旧PID57866，formal_window_recovery/operational_health/kline_observations全部未加载。没有重启、迁移、生成真实预测或下单。
- **旧服务尚不能获得源码的新历史保护**；旧21:20回补任务仍可能沿用破坏性旧逻辑。因此下一阶段需优先审计启动副作用、迁移/备份/回滚条件，协调代理冻结后安排受控原服务部署，而非以测试冒充线上修复。
- 资金窗口修复已交原资金代理643381da-2d0f-40f2-84db-1eabdeeb836a，范围tenbagger局部/共享fund窗口helper/RankTab资金标签/测试，仍在执行；不碰其代码，不重复其运行中测试。预期报告`docs/main-fund-window-repair-20260914.md`；本轮717不包含它的最终验收。
- 全目标保持active。证券身份/ST/退市及回补8.8%队列、三模块共享因子和不可变漏斗、物理时钟、容量/退出时间外证据、受控部署及下个交易日前向验收仍需继续。

## 第三轮后续交接集成：资金窗口/连续流向与部署预检

### 资金分支交接
- 读取 `docs/main-fund-window-repair-20260914.md` 最新产物：五会话共用本地确认日历、逐日收盘源/接收/观察时钟；排行/预案/详情窗口、UI日期/零/负/未知与CSV已接入，dated_latest_not_pit明确不作历史PIT。
- 旧夹具初始175 passed/7 failed并未隐瞒。授权代理适配三文件，种独立真实合同日历/时钟，保留原始值和时钟不改写检查，不以放松生产验证使测试转绿。
- 代理最终86 passed，再扩展资金/明日预案 **660 passed / 33.39s**；上轮UI build7.39s/隔离E2E2 passed保留独立截图。父最终集成测试另跑，不能把660与父历史717/462相加。
- 父实际查看独立funds截图：三行分别显示近5会话0.00亿、未知、-1.20亿，当前资金未知与历史窗口分列，CSV导出成功提示可见；没有资金行裁切。截图为隔离fixture，不是生产数据，日期tooltip内容由代理E2E验证而非此截图可见。
- 资金分支尚未证明生产5日覆盖。缺日历/缺收盘源时钟的数据继续unknown，可能减少排名/可执行预案，不能宣称收益改善。

### 父扫描路径与新发现
- 详细说明 `docs/scanner-fund-window-repair-20260914.md`。全市场默认及单股score_stock改为同一窗口loader；非空金额提示不能绕过DB核验，显式{}继续不可用不回退，当前合格资金独立保留。
- API扫描开始即冻结资金截止，避免await跨15:00后窗口变化；事件附窗口日期/完整性与用途，原snapshot_time仍是完成时间。不是整个快照历史PIT认证。
- 修复“连续净流”实际统计散布同号天数的错误：仅最新会话起同向连续run，0/反向即断，金额只累加该run；5条全有效且加总有限。原3/5天、金额、等级评分门槛均未降低。
- 异动cache更新 `v20_fund_window_streak_v1`。父新34边界测试；初批156 passed，最后8文件 **462 passed / 20.48s**，bash-137/138 exit0均已收取。
- 父最终交叉（全部fund测试 + 雷达逻辑/监控/时段 + 单股预案/预案引擎）**955 passed / 47.17s**，bash-139 exit0，含最新v20和代理3个已适配旧夹具。父137/138/139全部收取，不能与重叠历史集合相加。
- 父四个Python修改文件AST通过；scheduler整体diff --check发现既有深度采集、涨停字段和K线docstring行尾空白，本集成未修改这些区域，不把该检查称为通过，不为格式清理覆盖并行改动。

### 原8000部署预检（未部署）
- 独立只读报告 `docs/deployment-preflight-20260914.md`，父已复核main.py lifespan及db/session.py init路径。
- 19:32–19:35原8000仍PID57866，由launchd `com.claw.dev.backend` KeepAlive监督；旧21:20 THS修复仍排程，20:00还有正式预测/深度/指数任务。
- 现有restart脚本会unset禁调度且可能波及前端，不可直接用来做安全smoke；kill单PID会自动重生。停写要先控制后端监督者。
- 数据库约13GB，空闲87GiB，旧9/2备份不能代替本次一致性备份。仍027，news表有而4触发器无，K线观察表无；必须备份→副本演练→精确030迁移，不能stamp/downgrade删证据。
- 发现额外启动副作用：指数历史reconcile仍可修复StockDaily无效旧行；在本目标“不改写历史”约束下，需先隔离/保护该入口，或保持调度禁用。init_db即使禁调度仍有DDL，不能假称只读启动。
- **没有本轮重启/停服/迁移/订单/历史写入**。下一阶段优先修指数旧行写入与受控发布；不足条件时宁可维护，不能让已知旧破坏任务因拖延继续运行。
- 其他待办：龙头装配两处和共振latest资金fallback；身份/K线全消费者；共享因子/不可变事件漏斗/物理时钟；未来费用分配和容量退出隔离研究。全目标仍active，未声称全部修复。

## 自动轮次4：历史指数保护及原8000受控发布

### 源码修复与回归
- `docs/index-history-preservation-20260914.md`：reconcile_index_history从补缺/覆盖坏行改成只读SELECT对账，原缺口不变，无write开关；真实65日窗口仍以本次实际观察时点追加输入/hash，仅forward可见。
- 进一步限定复核发现60秒实时指数失败后的历史日线fallback也能覆盖StockDaily。父局部修scheduler：非当日跳过、写前复用当前有效指数筛选、0/3也质量失败；通用StockDaily upsert在写入口严格拒绝非实际当天，防日界等待。
- 新7例覆盖stale/mixed/current实际链路及过去/未来/未知/字符串日期；增补只读调用者dirty/new不flush、旧行所有字段不变、实际观察可见边界。
- 新只读部署审计/比较CLI及4例：不用app初始化、只读URI+query_only+一致读事务，74表计数和27关键表完整逐行摘要，保护输出不可覆盖db/WAL/SHM；比较器拒绝count/内容/revision/触发器缺失。
- 回归101 passed（140）、117 passed（141）、最终6文件 **200 passed / 22.43s**（148），审计工具4 passed（147）；重叠集合不相加，均真实隔离测试。

### 已实际迁移及恢复服务（详单独记录）
- `docs/deployment-repair-20260914.md`记录操作链、私密备份位置、主库/WAL哈希、版本及局限。
- 先freeze源码、graceful bootout旧launchd后端57866并确认无监听/DB句柄；前端始终原5173/PID21381，不调用全服务restart，不kill-9。
- 保存约13GB主库+65.6MB WAL+SHM完整停写集合，原件/副本hash一致；恢复副本quick_check=ok。
- 恢复副本精确027→030+完整禁调度lifespan，前后原74表count、27核心全字段digest相同，新表为空。生产主库/WAL再次核验未变后精确迁移；six trigger bodies核验，未stamp/downgrade/改历史内容。
- 最终release归档SHA256 `272081acb93a03d191723210bdd03215a35ee620c94833289cbecc59177140c8`；当前277个app/迁移实文件hash吻合。新环境标记 `src-review-272081acb93a03d19172`；保留原实验/推送设置，自动日调权仍false。
- 原8000先禁调度启动PID67251，20:01:54 health通过（第一次读取太早connection refused，复核为就绪时序，未隐瞒）。生产迁移+smoke后原74表计数、27核心全字段摘要与冷备基线一致（152 exit0），包括8,745,863根K线和预测/新闻版本/订单/模拟账本选定表；其它47表只比较count不夸大。
- 此后正常停smoke实例（153 exit0），保存维护回滚plist，原标签以flag=0恢复已有调度。只读代理确认已定位StockDaily阻断已解除；正常到期评估/元数据/原有paper生命周期不是本次手工下单，不把它们当永久关闭调度的理由。
- **已实核20:07:56原8000新PID67367、running=true、49任务**；原5173不变。指数窗口already_verified复用原证据。正式恢复器20:06:49真实请求promotion_2000，candidate_build尚在进行，不把attempt_requested当完成（154 exit0）。纯SELECT batch-health已归档（155 exit0）；此前20:00Cron因维护未运行，未追造1510，后续attempt按实际时点保留。
- **20:10:28复核已完成新2000批次**：run86为20:06:50开始屏障，run87 as_of20:07:24、completed20:08:25，1486快照/17ranked/5actionable；恢复器already_completed。1510仍0尝试，不补过期历史。
- **上线后发现下一优先问题**：统一质量required-route仅声明部分路线，news_catalyst_start/oversold_reversal_start/pre_board_probe_start在新批次诊断为未声明/unknown，run87整体诊断invalid_persisted_attempt/unknown_candidate_route（虽生产batch gate=true）；竞价仍blocked、mainline和second_board各自passed。下一阶段应核查合法路线和已有独立门禁再统一契约，不能强改通过或回写run87。这也是此前盘中多数旧批次invalid诊断的可核查因素，不等于全部已有门禁缺失。
- 恢复后订单2258/成交293/模拟成交110/账户13计数与维护前一致（仅count）。父140–156后台全部完成已收取；全字段严格不变证据是前述禁调度迁移/启动验收，不延伸为未来运行永不变化。
- 未手工下单、降低阈值、调权/晋级或重写冻结9/14复盘证据。上线不等于有完整交易日前向效果、更不证明收益提升；全目标仍active。

## 第5轮：逐路线契约与剩余扫描资金消费（源码阶段）

- 已完成合法路线与冻结证据解耦：新共享route_contract注册15合法名/13当前生成名/2保留原值兼容名；基础数据依赖统一，原B/C/D子集不扩张。新审计及run冻结route_contract版本，旧缺门不追认。正式非法路线在过滤前拒绝，独立尝试追加阻断、原台账不改。
- batch-health v2拆分路线身份与当时gate。20:29:15只读实际run87（1486快照）已验证结构issues为空、三合法缺门仍unknown、竞价仍blocked，整体completed_route_gate_unknown；此前invalid/unknown_candidate_route为旧解释。全字段两次摘要9b241773fd30e934b663ba849092b0a64aedec14403181de46a16958bfaa6cb0一致；产物outputs/repair_validation_20260914_round5/run87-route-contract-readonly.json。
- B/C/D取消“仅batch true、无route gate”的旧放行兼容；同run缺证/未知版本/损坏声明失败关闭，不回退旧run。保留旧明确route位值但标legacy_unversioned，独立概率/确认/可成交/风控不变；可能减少可消费候选，不是收益改进承诺。隔离配额研究也复用同冻结合同，只读三臂不变/买入始终false。
- 资金子任务修复单/批量龙头组装与共振资金消费，未知不再当0/不再获得+5资金分，真实零/负保留。盘后合法日期快照单独标display_only，缺时钟的板块资金只可research_only。父补首次await前固定cutoff、龙头缓存v6及API可空资金/质量/独立副本输出。父看过隔离截图，四类资金语义无遮挡。
- 最终父大组 **2501 passed / 183.22s**；此前定向路线460 passed、父资金/质量/新闻联合332 passed。父前端build **7.11s通过**，原5173业务fixture隔离E2E **19 passed / 18.5s**。子资金扩大940 passed属重叠验证，不把多轮重复累加作独立样本。中间旧fixture错误及适配详见两个专项doc，不通过放宽生产规则换测试通过。
- 追加旧未成交委托对不可变质量失败的取消/不借未来批次身份断言后，路线/概率/仓位事务399 passed / 13.41s（与大组重叠）。本轮验收和源码哈希归档outputs/repair_validation_20260914_round5/final_manifest.json，仅本轮阶段验收。
- 本轮后端未再次部署，20:43:42 ps再次确认原8000 PID67367仍round4部署；本轮pure diagnostics的实际库只读验证不是新API已上线。没有补run87/过期1510，也没有手工下单或更改模型/阈值。
- 下阶段仍需证券身份一致性、全部K线消费者质量、可复用前向因子/候选执行原因链、未来退出费用守恒和容量/退出隔离证据，最终统一部署及真实新交易日前向观察。本轮不能宣称全系统问题已修复。

## 第6轮：身份风险投影及状态误解禁修复（源码阶段）

- 在既有StockTagger统一代码/板块、名称、当前风险标记和有效黑名单判断，交易前风控、risk/check及信号过滤共同消费；缺失/冲突身份不能默认可交易，quote名称只增加风险，原候选名不能掩盖当前ST。标记known_projection不代表官方身份/PIT，新字段execution_authorized恒false。
- tag_stock/batch_tag不再用缺省False清除风险；映射更新保留既有ST、停牌、退市、IPO及未提供的涨跌停字段。调度_update_stock_status废除列表缺席自动摘帽/复牌和DELETE+INSERT；保留全部既有普通tag、人工名单及开始/结束日期，只追加/合并正向风险。四查询解析严格校验，坏批拒绝；自动解除始终0。
- 正向合并成功与全量覆盖分开：状态coverage_verified=false/positive_merge_status=ok，未证明分页总量和逐代码解除时保持degraded，不让DataQualityGuard缺expected_count默认的100%伪装为完整健康。后续需要真正解除证据/流程，不以查询无记录自动放行。
- 实际库只读21:06:59：002743旧tag富煌钢构/is_st=0但spot ST富煌，新投影禁止；600228旧tradeable但is_delisting=1+黑名单，新投影禁止。688835/601123仍缺身份/行情，不猜代码名称。5228标签/185名单/5225行情名称，85名称冲突、2未知代码未改写；双表全字段摘要027162ca83a4869e97276e8a304af77d0f2f4620ffd113f31f09089cd515bb43在只读一致事务内保持不变。
- 跨模块扩展 **2081 passed / 181.17s**；最后健康语义与日界/旧名单新增边界由6文件 **139 passed / 15.16s** 验收（重叠不相加）。新身份文件68例，详细版本时序见docs/stock-identity-risk-repair-20260914.md及本轮final_manifest。期间发现成交和ranked候选旧fixture只造spot不造身份，已显式补正常身份fixture；保留原T+1/成交排队/版本取消/概率门槛断言，没有放松生产准入。
- 原8000仍PID67367（20:06:43 round4发布），第5/6轮未部署，未手工下单、配置改阈值或写实际库。前端无本轮变更。
- 全目标仍active：需继续证券目录前向证据/缓存与原始消费者、RiskEngine异常/执行旁路检查、全部K线质量、全链因子和物理时间、费用/NAV及隔离容量退出研究。只读子任务本轮审查费用/NAV与身份边界；未收到结论前不宣称独立验收完成。

## 第7轮：风控异常与正式执行边界（源码阶段）

- 已修RiskEngine规则抛错却因未追加warnings/block_reasons而最终pass的漏洞；坏RiskDecision返回在加入结果前校验。异常/无启用规则/无效方向有engine类别和evaluation_errors，买卖均失败关闭，hold仅警告。checked_rules不再计入合成诊断。
- trading.submit_order内部命令方向统一规范化写回cmd.side，防止局部side=buy而实际RiskContext仍是BUY导致分支跳过。HTTP原有小写pattern未变，不夸大为其校验失效。
- 提交、普通下一轮与涨停queue复核共用有效风险汇总；缺失/无效结论、声明incomplete和pass却有block_reasons均阻止成交。保留已有warn策略、FIFO/五档/版本/确认、T+1及费用规则。
- 定向含两条真实待成交复核流程7文件194 passed / 21.19s；最终扩大1838 passed / 1 skipped / 166.36s（冻结账本测试需要显式样本）。此前大组9个正例实际空规则链，已仅给这4函数/6账户参数真实注册原规则+健康情绪+独立身份，保持原执行断言，220项定向复验通过。各组重叠不相加。新增63边界用例无实际订单，broker断言未触达且原候选证据不变；随后显式原9/14只读冻结库补跑skip项1 passed / 1.50s，13账户/现金资产残差/原文件SHA核验通过，不等于未来费用问题已修复；详细版本/命令见专项doc和manifest。
- 已确认待修公开paper_buy/paper_sell绕过submit_order直接记账；不能在持锁回调内直接套service导致递归/死锁。另起只读入口审计供下一轮拆分公开委托和私有成交，_PAPER_FILL_CONTEXT有值本身不是可信授权。
- 本轮源改动只在risk/engine.py和trading/service.py；原8000仍round4/PID67367，本轮不部署/不改配置/不改实际库及历史证据。全目标仍active，细节docs/risk-execution-boundary-repair-20260914.md。

## 第8轮：公开委托/私有落账分离（源码阶段）

- /paper/buy、/paper/sell改走统一submit_order，保留实际成功响应并关联TradeOrder→TradeFill→PaperTradeLog；拒绝不200伪装成功，旧精确幂等仅回原凭证，不补历史订单。stop/板块/首次建仓时间、禁止跨版本加仓、旧仓退出版本与原T+1/费用率保持。独立手动锁避免递归持有落账锁。
- broker只调用无路由的私有_book_paper_buy/_sell。新的paper_authorization绑定当前Task/DB/同一请求及冻结字段，一次消费broker和ledger阶段，异常关闭；3个service成交调用点统一dispatch。伪造轮次/Challenger上下文、直接调用adapter、跨Task/DB/对象/参数和重复消费均拒绝。不是抵抗任意Python代码的安全沙箱。
- 两类公开HTTP paper入口均检查精确健康QuoteRound、已确认本地日历、连续竞价时段、三时钟无未来/乱序、原90秒新鲜度、涨跌停/有限有序盘口与原参与率五档深度；整笔限价可成交才模拟落账，实际VWAP非自填限价，不排队/部分成交/市价假成交。两入口无条件禁止冒用Challenger账户。
- 服务前新增非可配置身份硬边界，禁用blacklist/observe_only不能给缺失/冲突/受限身份开新仓；卖出仍允许风险退出，但明确停牌始终不允许模拟成交。
- 只读实际库22:01:43确认9/14日历True/full，最新15:05:02轮次仍degraded（fresh_source_coverage=0.4947<0.9500）；不为新门禁改旧质量。原8000 21:57:06仍PID67367/20:06:43，未再部署。没有前端改动、手工下单/配置变化或历史证据写入。
- 本轮最终联合回归 **1906 passed / 166.40s**（bash-192），显式原冻结账本只读fixture亦通过且SHA前后不变，无skip，只有既有python_multipart警告。新边界67例；11文件AST及3个dispatch/两个私有路由分离断言通过。此前旧fixture错误与定向复验详见docs/paper-public-entry-repair-20260914.md；各组重叠不相加，全部父bash-186–192已收取。文件哈希及实际只读清单见outputs/repair_validation_20260914_round8/final_manifest.json。
- 全目标保持active：内部旧即时撮合默认分支仍需统一；预筛与落账局部身份、费用/partial-rebuy/NAV/物理提交钟、跨账户同轮深度与同预算容量、K线共享因子/不可变漏斗与时间外退出研究，及统一受控部署/下一交易日采证均未冒充完成。

## 第9轮：后续卖出买费分摊及成交重复认领（源码阶段）

- 修复部分卖出后再加仓的历史总买量分母错误：买200付5、卖100分2.50、再买100付5、最后卖200应分剩余7.50，旧公式6.67。新remaining_entry_fees_v1按完整账户/代码数量链和剩余费用分币，尾笔耗尽余数，不向现金二次扣费；费用率/持仓成本公式/阈值不改。
- 缺失/非法/未来交易、库存矛盾、新凭证冲突失败关闭，不默认零；旧无凭证卖出仅经济回放且明确未认证原政策，不回写旧realized_pnl。原只读accounting_snapshot经济回放公式不动，新分币凭证独立附加，历史桥接差异未强制抹平。
- 新PaperSaleAccounting与本次新卖出共享ledger提交，唯一成交ID、输入hash、费用前后和实际记录时刻；SQLite防UPDATE/DELETE/REPLACE三guard。源迁移031仅建表保护，不回填、不允许破坏性downgrade；发布审计新增表核心摘要，031须显式目标及9guard，默认030不变。TradeFill后续提交的跨表窗口和物理COMMIT钟仍未解决。
- 另堵住新委托键借相同signal_id把旧幂等账本回报再包装新TradeFill；相同委托键重试保持原结果，旧历史不补订单/时间。
- 22:25:35实际库只读一致快照：schema030、110交易完整行摘要32973133349453a07aa1375e43ea18babbd5a71cbb941c4919290b4062980ac9前后相同；9未平仓费用基础known、各5元共45元，均无本周期此前卖出。仅费用核验，所有execution_authorized=false，不代表可成交。22:36:31原8000仍PID67367/20:06:43，未迁移/重启/下单。
- 最终联合 **1967 passed / 166.20s，无skip**（bash-199）；原冻结账本SHA前后验证亦通过，只有既有python_multipart警告。新增费用50例/031迁移2例、发布比较2例；12文件AST及纯模块不初始化app DB通过。中间重复认领漏洞和旧fixture缺真实买费链导致的失败、修复与复验详见docs/paper-entry-fee-repair-20260914.md；测试组重叠不相加，父193–199全部收取。
- 本轮产物outputs/repair_validation_20260914_round9/current-entry-fee-basis-readonly.json及final_manifest.json。前端无变更，全目标active；内部即时撮合、全链不可变因果/PIT、共享因子/K线全消费者、容量退出研究、统一部署和下一交易日前向采证仍待继续。

## 第10轮：内部即时模拟成交统一预撮合验收（源码阶段）

- 所有paper即时分支在service无条件复用原公开行情验收器；require_immediate_quote保留兼容、默认True但False/None/0等不能跳过。defer提交/下一轮reconcile及封板queue/FIFO保持，不把submitted当filled。
- 即时v2合同要求同一健康QuoteRound、原decision_round及as_of匹配；保留原轮次汇总as_of，不能拿个股源钟替换。全部DB await之后再取实际服务器钟，过期/回拨/跨日/午休或14:57以后拒绝；实际限价内原参与率五档VWAP执行，风险/T+1/旧仓版本/费用不改。新增paper_immediate_execution证据，保留旧paper_public_execution响应键。
- 首次联合1954passed/4failed为旧正例缺日历/轮次/深度和固定执行钟；仅补明确隔离fixture，不放松生产规则。5文件定向340passed/46.50s，最终联合 **2025 passed / 182.70s，无skip**（bash-203），原冻结账本SHA前后亦通过。新增58边界、7文件AST/不可选guard结构验证通过；各轮重叠不相加，父200–203全部收取。详情docs/paper-immediate-execution-repair-20260914.md。
- 22:48:54原8000仍PID67367/20:06:43；22:52:27实际库只读schema030、新费用表尚不存在、110模拟交易/2258委托/293回报。全部交易行compact-JSON摘要84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357在同一快照前后相同，算法不同于上轮tuple-repr，不跨算法比较。本轮无生产下单/迁移/重启/历史改写或前端修改；第5–10轮未受控发布。
- **仍须修复**：service验收后broker/私有落账会等待_TRADE_LOCK和其它DB await，冻结filled_at不能证明锁后仍可成交。本轮仅完成强制预撮合，不能宣称整个即时可成交链无时间窗漏洞。下一优先绑定授权证据做锁后/变更前再验及失败事务验证，随后全链提交/PIT、容量/共享因子与K线、时间外退出研究、统一受控部署及真实新交易日采证。全目标active。
- 产物outputs/repair_validation_20260914_round10/current-deployment-readonly.json及final_manifest.json。只读子审查结论尚未收到已请求停止，不计为独立通过。

## 第11轮：即时落账锁后及变更前时钟（源码阶段）

- 将service原已验收的代码/方向/账户/价量/轮次及冻结时效JSON绑定同Task/DB/请求作用域；两个私有落账函数在取得_TRADE_LOCK后首句、及全部准备查询结束后的库存首次变更前各做同步时钟检查。过期/回拨/跨日/午休收盘边界/身份不符/损坏证据拒绝，不借后来配置延长该笔原有效期。
- 通过时新PaperTradeLog.trade_time与TradeFill.filled_at均用变更前逻辑时刻；原dispatch/锁后/变更前钟和输入hash随raw_json及risk.paper_ledger_timing保存，physical_commit_at明确null。T+1、旧仓版本、费用、确认及策略阈值不改。只检查原accepted quote时效，不宣称重新验收最新盘口/身份变化；deferred/queue专属锁后适配仍待继续。
- 定向原186passed/31.01s；新fixture早期20passed24failed（62.04s）及单例失败源自ORM equal updated_at触发onupdate实际钟、延迟钩子误作用风控阶段，已局部修测试帮助函数和注入范围。其后新50passed/11.20s。扩大207引入禁止日历loader检查时2061passed/14failed/14同例teardown errors（203.29s），确认trade_days_between即使缓存完整仍无条件loader；最终非日历测试明确提供本地fixture loader/禁止网络sync，不修改生产日历来放行。
- 修正后158passed/29.30s；最终联合 **2075 passed / 218.72s，无skip**（bash-209），原冻结账本完整文件SHA前后亦验证，仅既有python_multipart警告。8文件AST与两私有函数锁后首句/变更前无await结构通过。各组重叠不相加，父204–209已全部收取。详细契约、失败时序与保证范围见docs/paper-ledger-clock-repair-20260914.md。
- 23:09:17实际库只读schema030、110模拟交易/2258委托/293回报；全部模拟交易compact-JSON摘要84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357在同一快照前后且与第10轮同算法相同。原8000仍PID67367/20:06:43。未受控发布/迁移/下单、未改历史市场预测交易证据或前端。
- 全goal保持active。下一优先ledger→TradeFill事务归属及异常后半笔提交风险；物理COMMIT/PIT、deferred/queue锁后边界、当前身份复验与同轮容量、K线共享因子/不可变因果链、时间外退出研究及最终031受控发布/真实新交易日采证仍未完成。产物outputs/repair_validation_20260914_round11/current-deployment-readonly.json及final_manifest.json。

## 第12轮：三条模拟撮合链的原子记账（源码阶段）

- 即时、普通延迟、涨停排队均由service持有同Task/DB事务与原成交锁，账本/仓位/费用证明/TradeFill/委托累计同一最终commit。账户helper在事务域只flush；checkpoint先保存预检和委托再等锁，不声称整个请求零写入，避免SQLite写锁与Python锁反向等待。
- 包括取消在内的异常回滚；仅broker业务错误在回滚后另记拒单，receipt/commit异常传播，不拿丢失的提交确认覆盖已成交委托。锁等待后重读并核对执行身份、价量、委托类型、状态、累计和决策出处；旧孤立成交测试显式复现旧提前commit，历史拦截不删除。
- 初次扩大2092pass/39fail暴露29旧fixture缺accepted、6真实日志消费者访问回滚后过期账户、1测试未refresh过期订单及3新fixture冻结时钟错误；已分源码与测试修正，5文件复验188passed。父owner测试首1pass10fail因漏必填order_type，补fixture后11passed，随后扩至22个边界；未放宽生产执行或风控。
- 联合2219passed/252.32s（bash-220）后，因补全锁等待比较字段与11例边界再次完整复验：**2230 passed / 264.48s，无skip**（bash-221），只有既有python_multipart警告。新增133故障注入+22所有权测试，11文件AST/三路径原子结构及最终源码SHA前后相同；原冻结数据库完整SHA前后也通过。各轮重叠不相加。父210/212/215/216/219/220/221均收取完成；代理已请求停止，其测试由父联合回归覆盖，不把未收到的完整子审查报告计为独立通过。产物outputs/repair_validation_20260914_round12/current-deployment-readonly.json和final_manifest.json。全goal未完成。
- 23:29:23只读实际服务仍PID67367/20:06:43、schema030、110交易/2258委托/293回报，compact-JSON账本摘要84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357同快照前后及与上轮相同。未发布/迁移/下单或修改旧复盘证据、前端。
- 详细机制与保证边界见docs/paper-atomic-execution-repair-20260914.md；并发拒单/未知提交结果的上层解释、物理提交/PIT、延迟/排队锁后时间、共享因子/K线质量与全链因果、容量/退出隔离评估及031受控发布和实际交易日验收仍待继续。全goal active。

## 第13轮：拒单CAS与未知结果传播（源码阶段，验证跨至9/15凌晨）

- 即时/普通延迟broker明确失败后的拒单，改用原27字段检查点的单条SQL条件UPDATE，持原成交锁、拒绝携带未归属ORM变更；并发字段改变或真实partial已成交则409待核对，不用旧失败覆盖新订单/经济事实。未实现跨进程revision/ABA防护，不宣称全系统exactly-once。
- 原子事务/CAS失败保留原异常类型并附带进程内待核对标记；自动卖出不再将提交确认丢失伪装skip_sell/blocked，向上抛出使原账户风险失败门禁阻止本轮新开仓。明确拒绝且CAS成功仍正常解释。Challenger在rollback后显式刷新过期账户/事件/先前退出日志，防MissingGreenlet，不更改策略、阈值、权重或历史事件。
- 新CAS44例、自动卖出真实事务传播10例、Challenger调用方2例。初始296pass；CAS44pass；子fixture首次3fail7pass是缺round.records，补同一临时库真实行情记录后10pass。首扩大2282pass/4fail为午夜旧日期混用3例和新测试跨pytest事件循环锁1例；仅修本地日期/前交易会话/资金时钟与每例锁（中间46pass1fail暴露未同步资金钟），最终定向59pass。
- 最终联合 **2286 passed / 265.08s，无skip**（bash-229），仅既有multipart警告；8文件AST及同次输入SHA前后核验，原冻结账本SHA也通过。独立扩展旧test_strategy_iteration_challenger.py与新2例却仍 **21 passed / 12 failed**（bash-224）：两例诊断重现fixture空风险链被正确拒绝，不能假称修风控注册后行情链必然合格。此31旧例不在2286组内。下一轮优先补真实风控/同决策日历和行情证据后逐条定位，保留原断言，不能关闭风险来绿灯。
- 00:02:49原8000仍PID67367/9月14日20:06:43、schema030，110交易/2258委托/293回报。完整交易compact-JSON SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357同快照前后且与第10–12轮相同。无生产订单、迁移、重启或历史改写；无前端改动。旧Challenger组触发的日历同步仅写conftest临时库，不能算隔离无网络通过。
- 详见docs/paper-rejection-cas-repair-20260914.md；成功和失败日志、只读运行清单及最终hash均在outputs/repair_validation_20260914_round13。父222–229收取完成；子任务已请求停止，其10例由父最终回归覆盖，不把尚未收到的最终agent消息冒充独立整体审查。全goal active：旧Challenger失败、延迟/排队锁后和物理提交/PIT、共享因子/K线/不可变原因、容量退出对照及031受控部署/交易日前向验证仍待继续。

## 第14轮：Challenger执行资格回归与延迟轮次绑定（源码阶段）

- 上轮旧Challenger12失败已通过显式非autouse的真实执行fixture修复：原10条风控规则、本地同决策交易会话/健康情绪、既有价格深度不变的owned轮次与固定验收钟，不用空风险链或默认墙钟假成交。原31例31pass；28函数181条assert AST完全不变。只对原需要实际成交的9函数12参数例及continuous的3函数选择fixture；新25例连外层风控也真实运行。
- 新25例覆盖A2/B/C/D/F2五路线真实交易链、原限价VWAP/唯一回报/正确账户/原键幂等；缺日历/坏QuoteRound/未来接收钟15例无成交、跨source到账户5例拒绝，原confirmed完整行不变。4个strategy_iteration与连续性/rollback/买点hooks扩展185pass，新合同25pass；不宣称这些输入能证明策略收益或生产配置合格。
- 根据只读审查B1，修service.reconcile_paper_deferred_orders显式round_id与实际上下文非空却不同的漏洞：撮合入口查询前409，禁止把A轮价格标成B轮成交；同ID/省略参数/expire_only保持。修前新9例4fail5pass（4例未抛HTTP）；修后与路线/行情/原子性组合220pass。仅这个局部源改动，其余protected paper/auth/Challenger源码hash不变，无订单/历史证据回填。
- 最终完整联合 **2474 passed / 293.60s，无skip**（bash-235），包含前轮曾排除的Challenger文件；仅既有multipart警告。34个新增边界、9文件AST/同次SHA前后相同，原冻结复盘库SHA前后相同；各组重叠不相加，父230–235全部收取。成功/失败日志、原assert基线、只读清单与最终manifest在outputs/repair_validation_20260914_round14；主doc为docs/challenger-execution-round-binding-repair-20260914.md。
- 00:30:25实际原8000仍PID67367/9月14日20:06:43、schema030；110交易/2258委托/293回报，完整交易compact-JSON SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357同快照前后及与第10–13轮相同。未迁移/重启/业务API下单/改历史或前端；同PID不证明懒加载模块完整版本，仍须受控发布验收。
- 子只读静态审查docs/deferred-clock-audit-round14-20260914.md已读取冻结并请求停止，未把它当pytest或生产实证。其中B1本轮随后修复；A锁后时间、B2轮次逆序复用、B3缺ID、C最新身份/风险、D共享深度/queue开板量及封板FIFO近似均仍待独立修复。下轮优先延迟/queue的冻结合同和真实锁后钟，保留原原子事务/退出T+1/CAS/风险；全目标仍active，还包括共享因子/K线/不可变原因、容量退出时间外对照及031受控发布/真实交易日前向采证。

## 第15轮：延迟/排队真实账本时钟（阶段源码验收）

- 在原service/public_execution/authorization三个生产文件局部扩展pending_paper_fill_timing_v1_20260914；复用原Task/DB/单次scope与原子事务/CAS。缺合同不得真实落账；真实三钟/本地full日历/Tencent QuoteRound及上下文版本匹配，原decision非空且不同实际fill，decision严格早于commit。风险/原路线/深度或排队触发后再读真实钟，跨原session/期限不成交。
- 锁后与全部准备await后的before_mutation均守原quote有效期、适用原确认TTL/queue截止、同日/单调及连续竞价。成功TradeLog/TradeFill与queue诊断用最终逻辑钟，原决策/as_of/旧partial不重贴；逐笔raw存完整冻结合同及对应SHA，physical_commit_at仍None。queue缺接受或回报时不得确认成交。
- 新pending106例+即时空合同1例。阶段五文件258pass/47.52s、八文件原子/CAS/unknown等442pass/129.59s，新增queue4例独立4pass/4.03s；组间重叠，不相加。初次全联合2513pass/61fail/335.59s已保留，10个新fixture问题及父负责17个旧输入问题已修并复验，剩余34个旧T/continuous/confirmation fixture由独立代理仅在3个tests适配，**父尚未完成接收/全套重跑，不报全绿**。
- 所有失败如实记录：首次QuoteRound fixture漏NOT NULL字段不是生产漏洞复现；时间格式、新配置触发原版本拦截、provenance context少name均由补齐测试输入处理；未mock新validator或降生产门槛。只读审核尚未回收，不冒称独立通过。
- 01:02:26原8000仍PID67367/9月14日20:06:43/schema030；110交易/2258委托/293回报，全交易SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357不变。未迁移/重启/下单/生产业务API/历史或前端改动。primary docs/paper-pending-ledger-clock-repair-20260914.md，阶段证据outputs/repair_validation_20260914_round15/stage_manifest.json。
- 下轮先收两代理并集成完整回归；其后严格轮次推进/共享容量/queue量、锁后最新身份和完整风险独立修复，原decision轮次全集/物理COMMIT/跨进程、共享因子/K线/不可变原因、时间外容量退出研究、031受控发布/真实前向证据仍未完成。全目标active/armed。

## 第16轮：旧fixture集成验收与逐委托轮次推进（源码阶段）

- 已读代理3个测试文件/独立doc/113pass日志，父完整联合2581pass/335.09s，接收上轮剩余34个fixture；真实T+1和原历史候选/费用/日志断言保留，只有明确派发分支补本地证据和按新调用链精确增加quote计数。代理修改已请求冻结。
- 隔离真实账本复现同一委托B→C→B、换ID旧/不推进源钟仍fill，及缺坏既有证明仍继续partial：原新16例14fail/2pass，失败输出包括实际filled/partial而非仅预检错误。只改paper_public_execution.py，复用_order_fills核验逐笔冻结合同/ledger SHA/原decision/代码账户价量/逻辑钟、累计数和last轮次，拒重放及三钟不严格推进。缺历史证据waiting，不改旧账、不用新行情补造历史；不宣称跨账户容量或真实FIFO已解决。
- 新24例覆盖正常前向、上述重放、缺合同/钟/SHA、累计数/last轮次不符、缺回报与错证券。原子/CAS/时钟组299pass/99.34s，扩展新24+代理3文件137pass/19.64s；最终完整联合 **2605 passed/331.22s，无skip**（bash-256），仅既有multipart警告，组间重叠不相加。9源码输入AST/SHA前后与日志byte核验、冻结原复盘库SHA在round16 final_manifest。
- 新前台限定点只读审查未发现可证实新增明显漏洞，明确未跑测试/DB/网络及非部署证明。上轮未送达后台审阅未冒称接收，已请求停止；不再扩大其任务。
- 01:18:39原8000仍PID67367/9月14日20:06:43/schema030；110交易/2258委托/293回报，交易全列SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357不变；当次无submitted/partial paper单。没有受控迁移/发布/下单/历史改写或前端改动。文档docs/paper-pending-round-progress-repair-20260914.md，证据outputs/repair_validation_20260914_round16。
- 总目标继续active：下一优先跨订单/账户共享盘口容量与queue量、锁后最新身份/完整风险，再整合031受控发布；原decision轮次全集、物理COMMIT/跨进程、共享因子/K线/不可变全链原因、同预算时间外容量/退出研究及真实交易日前向采证仍需完成，不能以本轮全测通过冒充全部目标或收益完成。

## 第17轮：排队开板整笔五档数量验收（源码阶段）

- 确认旧queue开板只验价就填满整笔：合格fixture基线29fail中25个负例实际filled、4个合法对照缺新证据；先前在下单后改参与率导致版本拦截的fixture错误也保留日志，不算生产漏洞。
- 局部修改public_execution/service：抽取复用即时盘口校验，queue开板按原五档参与率逐档整手验量，合法原限价内数量须覆盖整笔；不足继续waiting，不把现价/累计量当卖盘补量，仍按原涨停委托价保守记账。成功数量证据冻结进原pending合同及ledger SHA，失败诊断带轮次/观察时间；scope明确单委托可见深度，不冒称共享容量或真实FIFO。
- 新31例含25个缺/坏/不足负例、4个合格对照、等待后下一轮只成交一次及await不改冻结证据。只把原orphan的queued=True与pending_clock的queue-open实际成交fixture补足300股且不交叉；普通partial一手、sealed累计量、原时钟/原子回滚/费用断言未改。
- 首组138pass/34.36s，最终完整联合 **2636 passed/333.24s，无skip**（bash-260），仅既有multipart警告。8输入AST/前后SHA、日志byte及冻结库SHA归档round17 final_manifest；无前端改动，不宣称构建或UI验收。
- 01:37:41原8000仍PID67367/9月14日20:06:43/schema030；110交易/2258委托/293回报，完整交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357，当次无pending。未迁移、重启、下单、业务GET或改历史，未发布本轮源码。
- 文档docs/paper-queue-open-depth-repair-20260914.md。跨订单容量需按独立账户实验语义明确作用域，不能直接让Champion/Challenger互抢盘口；该有界只读代理报告尚待回收，不计入已验收证据。锁后最新身份/完整风险、原decision证明、封板FIFO近似、全链共享因子/不可变原因、容量退出时间外研究、031受控发布和真实交易日前向采证继续；总目标active。

## 第18轮：独立账户内的轮次容量守卫（源码阶段）

- 核验上轮8份核心输入SHA全部匹配，没有覆盖并行改动。新增paper_depth_capacity.py、局部service/public_execution接入：同账户名/同QuoteRound ID/同证券/同方向在原成交锁内按已提交回报累计已用档位，复用原参与率/整手算法；其它策略账户独立，买卖两侧独立。原计划不足时即时reject、待单waiting，不偷偷重价/换档，rollback不占量。
- 即时raw新增完整immediate_execution_evidence，普通deferred补冻结档位与总容量，开板queue沿用上轮合同；account_round_depth与ledger SHA冻结关联，原时钟、原子事务、T+1、费用、风险阈值及auth/broker/纸盘账本源码不变。不把单进程五档计划约束称真实交易所容量/FIFO。
- 初始24例23fail/1pass（18超额实填、5独立账户缺证明）；首组79pass。限定点只读前台审查发现先筛回报可变身份再验合同的遗漏，父新增6例实测5fail/1pass；改为委托归属与回报/冻结合同/ledger轮次交叉选择和校验后，新40+推进24共64pass。原后台作用域报告未收到、不计验收，已请求停止；修后未冒称第二次独立全审。
- 首完整联合2646pass/24fail为三个旧观察fixture的“100股但空档位[]/spot无档”契约问题；仅补显式档位量，原116条assert AST完全不变，三文件114pass。最终完整联合 **2676 passed/351.40s，无skip**（bash-267），仅既有multipart警告；所有失败日志保留。13输入AST/前后SHA、日志byte/冻结复盘库SHA与assert基线归档round18 final_manifest。
- 02:03:18及最终2026-09-15T02:09:45.182916只读原8000/PID67367/schema030，110交易/2258委托/293回报，全交易SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357保持。未迁移、重启、部署、下单、历史或前端修改。
- 主文档docs/paper-account-round-capacity-repair-20260914.md。仍有整行回报丢失/旧split孤儿跨委托完整对账、不同ID同源别名、封板FIFO/跨进程/物理COMMIT、锁后最新完整风险、原decision全集、共享因子/K线全消费者与不可变原因、同预算时间外研究、031受控发布及前向连续性待验收；总目标继续active。

## 第19轮：锁后真实风险复核与显式停牌投影（源码阶段）

- 先核验round18全部13个输入SHA，再局部改service/core.stock_tagger/risk.circuit_breaker。即时买卖、deferred买卖、开板及封板queue在原成交事务/锁内重新读取身份/黑名单/情绪和账户持仓，执行既有完整风险链；不接受预锁ORM缓存，不在锁后重建已关闭账户，新记录数值账户ID防同名换户。
- 原价格数量计划、warning门禁、股票阈值、T+1、深度容量、原行情TTL及最终ledger时钟保持。新锁后风险合同冻结入原执行证明/回报SHA，失败risk_blocked；None/回退/跨日/带时区实际时钟提前拒绝，绝不回退datetime.now或重打quote。显式有效黑名单停牌合并为is_suspended以堵卖出，ST/退市/普通人工买入限制不等同停牌。
- 合格基线37fail中29个真实新增成交、5缺复核证明、3仓位fixture被旧版本挡住；另停牌投影3fail/8pass含2个错误卖出。新回归最终105例（含24个risk await后坏时钟）；聚焦阶段128pass，最终完整联合 **2781 passed/383.50s，无skip**（bash-277），仅既有multipart警告。
- 首完整2706pass/40fail、适配440pass/16fail及更早fixture错误均完整归档。旧七文件308个assert中300个内容保持，8个变更仅risk调用次数/序列与异常变量取值；原费用/无成交/版本/轮次等断言保留，新增严格早拒判断。21份候选输入冻结SHA/AST、全日志byte核验、原复盘库SHA归档round19 final_manifest；后台审查未回报已请求停止，不冒称独立全审。
- 最终2026-09-15T02:45:25.240523只读原8000仍PID67367/schema030，110交易/2258委托/293回报，全交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357。未业务GET、下单、迁移、重启、修改历史或前端；源码未受控发布。
- 主文档docs/paper-locked-risk-repair-20260914.md。锁前旧待单账户初始化/旧记录缺数值ID、完整跨订单孤儿/删除回报对账、不同ID同源容量及封板FIFO/跨进程/物理COMMIT、原decision全集、不可变全链原因和共享因子/同预算时间外研究、031受控发布与真实交易日前向采证仍待验收；总目标active，不以测试数量代替部署或收益。

## 第20轮：跨委托账本—回报双向校验（源码阶段）

- round19的21个backend输入起始SHA/AST全部一致。新增paper_execution_integrity.py，并在service既有3个成交事务入口局部接入；即时买卖、deferred买卖、开板及封板queue在容量/锁后风险/真实Broker前校验当日同账户名、证券、方向的账本与回报。两表同时出发、SQL叶列读取，缺失/重复/经济或身份冲突risk_blocked，不再因整条回报缺失而释放另一委托容量。
- 保存paper_account_execution_integrity_v1_20260914及问题ID/成功配对摘要到原风险和执行证明，再复用既有ledger SHA。不重放或补造历史、不放宽阈值、不跳过原T+1/风控/轮次/深度/最终时钟和原子事务；独立策略账户仍各自实验。SQL异常/取消原类型传播并回滚，非当前原子事务拒绝。
- 原38例基线30fail/8pass，其中23个错误新增成交、6缺新证明、1重复回报已有旧容量阻挡；首次聚焦96pass。新增3个raw证据、6个锁等待真实另一会话修改、12个SQL/取消和1个事务外用例，总新增60。聚焦99pass/23.54s；最终完整联合 **2841 passed / 408.53s，无fail/skip**（bash-283），仅既有multipart警告。
- 首完整2820pass/2fail：旧隔夜fixture只改账本日期留下冲突回报，改为真实隔离9/11 HTTP买入再9/14加仓/卖出；其全文件543条assert AST完全不变。T回补未来行测试保留as_of原信号断言，明确未来买入无回报属于当前执行账本损坏、risk_blocked/零Broker，不冒称分支语义没改变。新增故障测试首91pass/8fail为预创建待单的ID排序误认首单，改按明确order_id核查当前委托，非修改生产行为。
- 03:01:07只读原8000/PID67367/schema030，110交易/2258委托/293回报，全交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357，9/15交易0。03:12:30历史关系进一步核查：10个无回报账本均closed default且日期4/28、4/30、8/31；193个无账本paper回报对应default名下filled委托，日期5/11–8/28。100个可规范关联对在所查账户名/证券方向/交易日/价格数量费税列无冲突；不是完整原合同或财务对账，更不能据此自动恢复。
- 最终03:18:46再只读原PID67367/schema030、110/2258/293行及全账本SHA保持，未部署/迁移/下单/改历史或前端。主文档docs/paper-cross-order-integrity-repair-20260914.md；25份候选输入SHA/AST及旧fixture差异、6份原stdout逐字节一致日志和历史只读SQL归档round20 final_manifest，原复盘库803618816字节及SHA保持。当前day/name/code/side门禁不是全历史账户修复，也不覆盖多锚点同时删除/一致改写、不同ID同源/封板FIFO/跨进程/物理COMMIT。其余原decision全集、不可变全链原因/共享因子、同预算时间外研究、031及第5–20轮受控发布/前向采证仍待验收；目标继续active。

## 第21轮：雷达候选原因不可变捕获（源码阶段）

- 起始round20全部25个backend输入SHA一致。新增AnomalyCandidateEvidence与032迁移，复用原候选record_id/信号identity；原AnomalyCandidateRecord明确为最新兼容投影，不再拿最后snapshot/拒绝理由解释first_seen。
- 局部API在第一次DB await前冻结本次候选/门禁；追加每次真实评估，A→B→A不被历史内容去重吞掉。记录baseline/入选/技术和观察消息/绩效过滤/单轮及小时预算阶段成员关系，保留各条成功、失败、缺回报及observation身份；当前reported_sent/not_dispatched等与此前pushed、已有SignalPerformance引用分开，不改推送额度或交易许可。
- captured_at是真实本地捕获日期，非当日刷新not_recorded且不更新旧投影；明确非原评估开始/行情首次可用/物理COMMIT和历史PIT证书。快照或gate非法unavailable；附加表缺迁移在savepoint失败不掩盖证据不可用、仍保留兼容表，外层提交失败全部回滚。ORM/SQL update/delete/replace保护，032不回填、不删除。
- 新33项隔离用例覆盖历史原因、各渠道/缺结果、缓存await突变、逐级6候选漏斗、缺表/非有限输入/外层提交、ORM及SQL保护与真实031→032 Alembic新表/已有表及重复upgrade。首组126pass/3fail为日期fixture未冻结及新增metadata；适配139pass。旧两文件1064条assert原节点和顺序保留，另4条metadata/缓存断言。
- 首联合1281pass/88.06s，补充边界后1286pass/1fail为非法gate列表仍调用get；修正异常分支后3项聚焦通过。最终完整相关联合 **1287 passed / 89.92s，无fail/skip**（bash-288）；35份backend最终候选SHA/AST冻结及验后匹配，两个部署脚本纳入候选全内容摘要及032+031表/触发器验证，默认030不暗中前进。
- 03:36:51只读原8000/PID67367/schema030，新表不存在；110交易/2258委托/293回报，交易SHA仍84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357。全历史候选33691条snapshot合计157635837字节，非新表单日增长估计；前向写入空间/延迟须发布前测量，不自动删证据。
- 最终03:45:04只读PID67367/schema030不变、新表未部署，账本/委托/回报/候选/绩效五表全列摘要与行数较03:36:51全部保持；35份源码AST/SHA、6份原stdout字节及冻结复盘库SHA验后复核归档round21 final_manifest。主文档docs/anomaly-candidate-evidence-repair-20260914.md。本轮只覆盖自动refresh候选评估末尾，不冒称完整事务outbox/发送崩溃恢复、纯B1/手动入口、原decision→paper/order全关联或前端历史浏览完成。原decision/共享因子全集、时间外同预算研究、历史对账、031/032受控发布和真实交易日前向采证仍待验收；未下单/推送/迁移/重启/前端修改，目标active。

## 第22轮：因子日线/真实资金输入统一（源码阶段）

- 实读发现单股与批量因子各取StockDaily最近30条，未检查请求日期/日历缺口；单股合并未经时钟验收的FundFlow，批量不读资金，且静默只算100只却称完整截面。不是把48个注册因子误认成48个已可用因子。
- 新market_inputs.py复用本地确认日历、K线结构/正式来源/原0.011价格链和closed_fund_status。30会话加1锚点；默认只用已完成日，明确非交易/未来/未完成日期拒绝；缺日保留空行，不用StockDaily/Spot填补，不回填/推断复权。资金真零保留，缺/未来/非收盘值未知；未存振幅和逐股上下文继续未知。
- 两计算入口共用同一帧，返回输入值SHA、逐日问题/原资金时钟、列及因子可用数；明确读取时投影、非PIT/非交易授权。批量保持100只运行界限，排序去重、显式deferred_codes/完整分母/失败及可用值数，不生成新排名/自动续批。存储服务未改，旧FactorValue只插入不存在行；本轮没有新增前向因子版本表，不能称48因子全部共享采证完成。
- 新55项真实临时SQL/ASGI边界覆盖两个入口一致、日期/源/缺口/零量/价格链、资金时钟/真零/缺细分、ASCII、旧因子不覆盖、no_autoflush、未来行排除和批量日历中途变化等。首次43pass/3fail为新断言误写MACD注册名，不是旧生产漏洞行为基线；修正后中间880pass、扩展55pass。
- 最终相关联合 **1072 passed / 42.17s，0 fail/skip**（bash-292），包含因子及共享资金/日K/日历和路线/容量/晋级排名研究回归。3条既有依赖/pct_change弃用警告保留，未改公式或掩盖。30份最终backend输入AST/SHA验后匹配；以旧函数体重建的三个原文件完整SHA一致，证明只改目标函数；4份日志逐字节归档。
- 04:06:27原8000仍PID67367/schema030，110交易/2258订单/293回报、factor_values/evaluation_run各0，五表全列SHA较03:59:49不变；原冻结复盘库SHA保持。未下单、业务API、迁移、重启、部署或前端修改，不用测试冒充利润/运行采证。
- 主文档docs/factor-market-inputs-repair-20260914.md；证据outputs/repair_validation_20260914_round22/final_manifest.json。下一步仍需逐股上下文与不可变前向因子采证/三模块关联、同预算时间外研究、完整候选→确认→订单原因、历史对账、031/032和第5–22轮受控发布及真实交易日前向验收；目标active。

## 第23轮：因子计算追加证据与隔离复算（源码阶段）

- 起始上轮30个backend输入SHA一致；未改目标之外的27份输入。新增FactorComputationRun/033，批次保留完整requested/attempted/deferred/失败分母、实际过滤后30行计算帧、明确空context、因子结果/置信度/元数据和已加载callable字节码/声明合同/依赖版本指纹。不是磁盘源码冒充旧运行，也不是签名构建/完整环境锁或原HTTP响应。
- 只接现有显式compute-and-store，单股计算API仍只读，不新增调度/自动研究/调权/晋级。计算前冻结帧，后验输入与实现不变；每股在下个await前冻结结果。原FactorStorageService和48因子公式不改；旧FactorValue insert-missing-only不回写历史。
- 新capture与旧格式新增值共同提交；缺033/flush/旧值写入失败不无痕存储。helper只报flushed_not_committed；提交成功才报committed。真实提交后ACK丢失仍抛错，可能已经落库，不把rollback说成能撤销该提交。capture_id非HTTP幂等键，新请求可追加新观察。
- A→B→A保留三次；同ID精确重试幂等、冲突拒绝。ORM/SQL update/delete/replace保护，033空迁移/已有兼容表修触发器，不回填、不删除；不兼容列/非空/主键/唯一性拒绝并保留旧行。部署脚本新增因子两表内容摘要及033+前置表/触发器检查，默认仍030。
- 显式ID只读与隔离复算核验SHA、列/JSON时钟身份、协议、分母/数值；实现版本不同不重放，调用方payload先拷贝，输入/实现中途变化拒绝，空样本matched=None。仍point_in_time_verified/trading_authority/promotion_eligible/automatic_weight_update均False；逻辑capture≠物理COMMIT或历史首可用，未给旧因子盖PIT章。
- 新46例（40捕获/复算、6迁移/部署内容）加既有测试。首适配54pass/1fail为空dict假计算不满足48分母；仅该旧容量fixture改为真实原引擎spy，保留100/101截断断言，实际4800旧格式行多数未知。新增初组36pass、综合104pass、首联合1159pass；补3例后的最终 **1162 passed / 74.78s，0 fail/skip**（bash-297），3条既有依赖/pct_change警告。两次edit非唯一匹配未改文件，随后唯一定位补齐，已纳入最后回归。
- 04:27:34原8000仍PID67367/schema030，033新表不存在；110交易/2258订单/293回报、因子值/评估运行各0，五表全列SHA较04:20:43全部保持。36源码/测试AST和验前后SHA一致、5份原日志逐字节一致、原冻结复盘库803618816字节及SHA保持。未生产业务API/迁移/重启/下单/历史或前端改写，未部署。
- 主文档docs/factor-computation-evidence-repair-20260914.md；证据outputs/repair_validation_20260914_round23/final_manifest.json。尚需逐股上下文、实际前向决策引用与雷达/晋级/纸盘复用、同预算时间外收益与容量退出研究、完整候选链/历史对账及031–033受控发布/负载/真实交易日前向验证；新表本身不是这些完成的证明。目标active。

## 第24轮：截面逐股上下文隔离（源码阶段）

- 复核上轮36个backend输入SHA一致。确认compute_cross_section旧代码向全部股票广播同一份kwargs；新回归改前3项失败。当前生产调用检索未发现该截面方法入口（单股API/显式存储用compute_single），因此不把这项潜在缺陷包装成9/14踏空的已证实原因。
- 同一既有引擎改为显式contexts_by_code；禁止共享kwargs，缺股为空、多余代码/错字段/容器提前拒绝。ASCII证券身份与date/DataFrame合同；首await前冻结成员/全部逐股标量，防后续映射串变。真零/负值保留，非有限未知，bool不转0/1。DataFrame仍调用方所有，不冒称帧或外部来源已验时点。
- news_avg_importance、limit_up_time、margin_balance_change_avg5声明为conditional_context，仍沿用原分支校验；全部48因子与基类calculate AST不变，compute_single/权重/原排名公式及稳定同分顺序未改。结果逐股附声明参数提供值/缺项，明确caller_supplied_unverified及四项授权/PIT/晋级/调权False，不伪称动态分支全部用到。
- 已加载实现描述包含条件声明；旧格式捕获可只读但错版拒绝复算，不改原记录。033日线存储仍context={}，不从截面内存借参数，不将v1偷偷扩成上下文采证；三模块真实来源/时点适配与capture引用仍未完成。
- 新99项边界（含48项逐因子声明核对及2个真实临时SQL），旧排名参数化fixture仅六个标签改ASCII代码，全部数值/方向断言保留。首740pass/1.81s；最终相关联合 **1261 passed / 75.65s，0 fail/skip**（bash-300），3条既有依赖/pct_change弃用警告。不是全项目/收益验收。
- 37份最终backend输入AST/SHA匹配、3份原日志逐字节匹配、原冻结复盘库803618816字节/SHA保持。归档脚本首次tuple/list形状比较误失败，规范化JSON表示后重验，未放宽源数据规则。04:45:59原8000仍PID67367/schema030，新033表缺；110交易/2258订单/293回报与因子值/评估各0，五表全列SHA较04:40:35全部保持。未业务API/下单/推送/迁移/重启/前端修改或部署。
- 主文档docs/factor-cross-section-context-repair-20260914.md，证据outputs/repair_validation_20260914_round24/final_manifest.json。另起只读发布就绪审计（subagent 7aae6a75-cdae-4f11-833a-8a8bdd387840，无文件所有权/服务操作授权），供下一轮核对原服务031–033与源码受控发布步骤。仍有真实上下文/决策链、时间外研究/对账、负载/部署/前向采证工作；总目标active。

## 第25轮：发布证据门禁修复（源码/隔离验收，未部署）

- 起始上轮37个跟踪backend输入SHA匹配。父实读发现旧发布compare空counts/digests配齐版本/触发器名就PASS；改前1项回归失败复现。旧工具还只看触发器名、不验新证据schema且漏了stock_kline_observation全内容摘要；不能因此反推原生产触发器已失效。
- 现有audit/check脚本加共享只读合同：v2审计在同一ro/query_only/BEGIN内取表计数、核心内容、完整表/索引/FK和触发器定义；加入K线观察内容摘要。保留Python3.11原repr摘要协议，严格拒绝旧/空/部分报告、非法类型/摘要/分母/错库/倒置时钟。副本→原库须双绝对路径pin；默认仍精确030，不盲升head。
- 门禁核对完整已知trigger SQL模板、031–033声明列/类型/非空/PK/唯一/查询索引/FK；既有schema/index及非本轮trigger不静默改，新表只准声明版本段且为空。异常只拒绝，不删表/修旧行或替代迁移。CLI限定无业务证据比较，并报告实际检查的新证据表数；030时0，不误称已验031–033。
- 新82项回归含真实临时030→033 Alembic及完整禁调度lifespan（禁socket连接/禁scheduler.start，仅进程内ASGI根/health）；真实前后摘要/结构一致，三新表及订单成交为空。首77pass/1fail因fixture只metadata缺022/024两个旧唯一索引；读迁移及生产schema确认已存在，仅补正确旧基线，不放宽门禁。继128pass、131pass；最终相关联合 **1343 passed / 79.21s，0 fail/skip**（bash-306），3条既有弃用警告。现有三测试文件四函数局部适配真实审计结构。
- 生产两次完整只读快照05:02:31/05:07:34：75表计数与30核心摘要；30核心全列摘要、75表schema、全部trigger定义一致，原PID67367/schema030，031–033仍不存在。唯一计数差为data_source_health 485381→485384，窗口窄查询看到3条新健康记录；未对健康表做全内容校验。严格CLI因此 **exit1/table count changed**，没有掩盖为全零变动/禁调度smoke通过；原调度本轮未停，正式发布必须在维护窗口重验。
- 49份跟踪输入AST/SHA验后保持、8份日志逐字节归档，原冻结复盘库803618816字节/SHA不变。未改app/main/db/session/迁移/策略/执行/前端，无业务API、下单、推送、生产迁移/重启/部署。一次JS转义工具未执行、一次CLI f-string转义运行前纠正，最终AST/联合均覆盖。
- 主文档docs/deployment-evidence-gate-repair-20260914.md，证据outputs/repair_validation_20260914_round25/final_manifest.json。此为发布工具就绪的一部分，不是完整发布包、原13GB库恢复演练或新运行验收。尚需冻结完整代码/私密配置/回滚单元、停原监督者/确认无写者、一致性备份与恢复副本演练、原路径031–033迁移/禁调度smoke及单独恢复既有调度；另需真实上下文/三模块引用/同预算时间外研究/对账和前向采证。先前只读agent尚未有本轮可采纳的结论，不作为独立审核完成证据；总目标active。

## 第26轮：真实13GB恢复演练、依赖与夹具隔离（未生产部署）

- 起始实际pwd确认Claw，原8000仍PID67367/9月14日20:06:43启动；先冻结完整backend app/alembic/scripts/tests+requirements/ini 558文件，私密归档/解包/工作区逐字节一致。另单独保全原.env和launchd plist，仅记录哈希/安全元数据，不打印凭据；没有动监督者、端口或全机依赖。
- 新snapshot_sqlite独立CLI：源mode=ro/query_only，SQLite在线一致性备份、私密0700/0600独占partial、空间/合作式时限、目标独立DELETE journal、quick_check/fsync/SHA后独占发布；不复制活动主库裸文件、不checkpoint源。18项新边界覆盖WAL已提交/未提交、错路径/覆盖/软链/权限/坏库/超时/空间/独立恢复；与旧发布合同100 passed/5.61s。
- 实际生产在线备份13,059,362,816字节，230.441s、quick_check ok、SHA85cf3384…5b6312；从封存备份恢复另一完整文件532.868s、quick_check ok。不是冷备、精确开始时刻或PIT；原服务保持运行，两个SQLite物理文件头SHA不同不代表内容不同。
- 首演练在连接副本前因私密HOME隐藏用户greenlet而失败，SHA/schema030确认副本未改；复用明确PYTHONUSERBASE后真实031–033迁移成功。导入urllib3的::1绑定探测被栅栏抓到，单独隔离子进程关闭IPv6能力探测，不允许绑定/联网/子进程。最终完整lifespan/root/health成功、调度0、禁用动作0，11项栅栏测试通过。只在独立副本，不是原服务升级。
- 严格v2双路径pin比较通过：75→78表、原75表计数/30核心全字段摘要保持、6原+9新保护共15个正文与schema合格，031–033三新表空。05:46:44封存备份SHA及原558输入保持。实际依赖（SQLAlchemy2.0.48/pandas2.3.3/numpy1.26.4等）与requirements声明不同，记录245个安装名/版本；未声称干净环境/声明版本验证。
- 随后局部修复三测试夹具的class/instance monkeypatch残留及费用正负例日历懒加载；新增3个嵌套夹具还原回归和1个依赖声明回归，requirements原SQLAlchemy版本仅加asyncio extra。改前2个夹具失败+1个依赖失败；首修254断言通过但7次DNS尝试，整体exit1；补齐共享费用日历后255 passed/70.81s、联网尝试0。旧费用拒卖/零账本/持仓断言不减，新增零成交回报。不改runtime app/迁移/公式/阈值/前端。
- 最终候选559文件归档SHA8537adee028ac2d1ed1f1396aa5191ac77d1400af0589f7f788390db6afa4d60；运行app/alembic/scripts与大库已验版本逐字节一致，只测试/依赖声明变化。53文件最终 **2658 passed/419.56s、3既有警告、0pytest fail/skip**，但额外严格栅栏仍捕获 **48次socket.getaddrinfo**，因此总命令 **exit1，全套隔离验收未通过**。短堆栈只到requests/urllib3，不猜各用例归属；下一轮用PYTEST_CURRENT_TEST/应用栈继续定位，不禁用审计。中间未修齐版本作业bash-323已SIGTERM取消并归档，不算通过。
- 05:54:05原PID67367/schema030保持；对照05:26:59，110交易/2258订单/293回报/因子值0/因子评估运行0五表全字段SHA保持；原冻结复盘803618816字节/SHA保持。仅此子集，不假称原库全部健康计数不变。16份中间原始日志tar逐成员SHA保全，完整记录失败/取消，最终日志另存。
- 主文档docs/full-size-release-rehearsal-20260914.md；证据outputs/repair_validation_20260914_round26/final_manifest.json。新增/修改7个backend文件，不生产迁移/重启/下单/业务API/历史或前端改写。仍需48次测试联网来源修复、独立依赖/正式发布停写窗口、真实前向连续性/上下文/三模块引用、完整链路、历史对账及时间外同预算研究；全目标active。

## 第27轮：48次测试联网归因与修复（未部署）

- 起始第26轮559个backend输入SHA保持。新显式pytest_no_network CLI在主pytest进程/线程阻止TCP/UDP/DNS尝试，依赖吞异常仍令整体失败；记录用例活动标签及64层文件/行/函数，不记录socket参数/地址/凭据。详细记录封顶但总计数继续，输出0600独占创建。不是OS沙箱，子进程不自动继承钩子。
- 原53文件重现2658 passed/441.18s但联网48、exit1。全部48次由public_env共享夹具的27参数用例触发：内部卖出6×4+18×1、公共T+1退出4、停牌卖出2×1。实际持仓天数→trade_days_between无条件加载日历，只填_cache不足。原库2026日历242条满足既有本地分支，不能把空测试库联网说成生产缺日历或踏空原因。
- 仅该既有测试文件public_env改类级本地加载器/严格年份缓存校验/禁源同步及teardown零调用；保留真实风险、报价、临时DB门禁和全部旧断言。新增12个审计器测试及2个真实日期窗口回归；新回归最初2F+2E因未调用风险检查，补真实10规则预检（不提交订单），干净改前2F复现全局日历连接。修复后4文件139 passed/24.42s，联网0、exit0。
- 本轮4个backend文件（一个旧夹具修改、三个新增工具/测试）。原模块除public_env外AST一致，其余558个旧输入不改。冻结562文件、归档SHA82e927357096a058f68d924254f95ab3bc947ed984c45e59d34db306dc0bcd20；工作区/归档/解包一致。最终55文件 **2672 passed/402.46s、3既有警告、0失败/跳过、联网0、exit0**（bash-332）。共224测试文件只选55，不称全项目覆盖；聚焦测试不另叠加计数。
- 06:28:32原PID67367/schema030保持；相对06:09:03五表110交易/2258订单/293回报/0因子/0评估全列SHA保持。原冻结复盘803618816字节/SHA保持，562候选输入验后匹配。6份中间失败/成功原日志及最终日志逐成员SHA归档。不改runtime app/迁移/前端，不重启/迁移原服务、不业务API/推送/下单/历史改写。
- 主文档docs/test-network-isolation-repair-20260914.md；证据outputs/repair_validation_20260914_round27/final_manifest.json。第26轮的测试联网缺口已关闭，但正式发布停写窗口/一致依赖、真实逐股上下文/三模块前向引用、完整决策链、历史10/193对账、同预算时间外路线/容量/退出研究和真实交易日采证仍未完成，目标active。

## 第28轮：可重建依赖与数据库池启动修复（未部署）

- 起始第27轮562份backend SHA全匹配。私密独立venv按声明安装失败：pywencai0.14.2本次PyPI无可用版本，原环境实际0.13.1。改为已用0.13.1，未改源分页/重试。原session向所有默认池传队列参数，现有SQLAlchemy2.0.48三种内存URL改前3F/3P；独立声明2.0.35下原冻结文件库导入亦exit1/NullPool非法参数。
- 新纯engine_options：文件库显式AsyncAdaptedQueuePool，内存StaticPool且不传队列参数；保留原池大小/超时/回收及SQLite busy timeout，非SQLite不传SQLite专属连接参数。session仅改engine构造，旧函数/类AST不改；无迁移、风控、账本、公式或前端改变。11项边界含5种实际临时连接/事务/池合同，当前环境11P后7文件461P/137.11s、联网0。
- 首独立声明环境100安装分发均在新prefix且user-site关闭，pip check通过；95P聚焦及独立13GB已033副本smoke/v2门禁通过。但完整56文件 **2664P/19F/404.85s**，19项全因真实IC延迟import遗漏scipy，联网0仍整体exit1；没有以pip check或健康启动冒充IC可用。
- 补直接SciPy1.17.1（与原环境实际版本相同，Python>=3.11、支持声明numpy2.1.1），不改Spearman/阈值或用伪IC降级。保留首版两个环境及失败证据；从98+1个精确SHA wheel创建第三个最终离线venv，禁止联网/额外进程，99轮子安装/零尝试/pip check通过；101个分发等于首版100+SciPy，24条直接声明全部满足、无user/global泄漏。轮子只验证本机CPython3.11/macOS arm64，不是跨平台/安全审计或独立Python镜像。
- 最终564输入归档SHAd26704b31ea8f950a6fdb7449f0a3708f0c684eab1d729b9240800a7f0537c32；相对首版仅依赖与测试断言变化，runtime app/alembic相同。最终新环境56文件 **2683 passed/372.66s、0失败/跳过、联网0、exit0**（bash-346），保留3测试摘要警告及loop_scope启动弃用提示；225测试文件仅选56，不称全项目。最终完整副本smoke2.443s/1连接/198冻结app模块/禁用动作0；78表计数/33核心内容/schema/15保护再次通过（bash-347）。临时030→033迁移在聚焦测试中；大库此轮仅已033副本再启动，不冒称原库升级。
- 06:57:56原PID67367/schema030保持；对照06:43:21及上轮，五表110交易/2258订单/293回报/0因子/0评估全字段SHA保持。原冻结复盘803618816字节/SHA保持，564最终输入和99wheel验后SHA一致。21份中间原日志含失败及最终联合另存，文档快照/哈希清单归档。4个backend文件改动，无业务API、下单、推送、历史改写、全机依赖修改、原服务重启或前端改动。
- 主文档docs/declared-environment-repair-20260914.md；证据outputs/repair_validation_20260914_round28/final_manifest.json。环境可重建缺口在上述本机范围验收；仍需独立受控发布停写/新备份/原031–033/禁调度对照/恢复既有调度及前向采证，还有逐股上下文/三模块引用/完整决策链/历史10与193对账/同预算时间外研究。只读独立审查暂无可采纳回报，不作为独立审核通过。目标active。

## 第29轮：原8000/原库033受控发布（已启用，仍有运行告警）

- 07:02核验最终564输入/99wheel SHA，保全当前.env和plist到私密目录，不打印凭据。旧实际发布272081...解包后，在独立13GB已033副本上验证旧源+原解释器禁调度回退：lifespan/root/health通过、禁动作0，78表/33核心/15保护前后v2对照通过。仅此禁调度代码兼容结论，不自动降schema或恢复旧弱交易行为。
- 07:08:54精确bootout原gui/501/com.claw.dev.backend，原PID67367消失/8000无监听/库及sidecar无持有者；不强杀、不清其他端口。SQLite备份API源ro/query_only，新当时独立备份13,059,387,392字节，183.287秒、quick_check/DELETE/fsync通过，SHA54b3adf31a4ca516d12e1f1d83184c06a86c2fd0a7a00546eabde364ad593ad8；停写前后观察成立，但不改标为历史PIT或OS无间隙写锁。07:14:37原库/新备份全证据双路径对照通过。
- 07:15:04–07:15:06原绝对DB精确031–033，网络/其他库/子进程guard尝试0、exit0；原75表计数/30核心全字段内容及原schema/index保持，三新表空，共15正文保护。未stamp/head/回填/改历史。第26轮旧备份和原复盘库继续保全。
- 临时维护ASGI门只准无query GET根/health及lifespan，其他HTTP503/WS1013；9项隔离测试通过，防前端带写业务GET污染验收。原8000禁调度PID90675、实际最终venv/6关键依赖/184已导入app路径SHA匹配、健康200/任务0/非业务探针503。07:18:47再次完整v2原库对照通过。
- 再次精确停维护实例/确认无持有者，独立恢复原调度0。最终plist相对原只变解释器到final-env-round28和版本标记src-review-d26704b31ea8f950a6fd；原.env/DB路径/实验/推送/监督属性保持。07:19:55原端口启用PID90785/49任务，07:22:20仍同进程；有当日正式next_run不等于批次完成。维护约11分钟、盘前而非零停机，不调用paper/trading/补批接口。
- 启用后两次仅8表ro子集全字段SHA：110交易/2258订单/293回报及五空因子/新证据表保持；不冒称所有活跃库表无自然写入。实际子进程90791 caffeinate -s -w 90785及pmset PreventSystemSleep断言成立；AC-only，不防合盖/电池/手动休眠。
- 真实发现07:20:25计划的auction_collect/intraday_fast在07:20:28各missed/迟到2.519秒，时间落于预热期间，尚无唯一阻塞函数归因。随后任务恢复但告警保留，operational_health=degraded；当时quote=not_expected，calendar_known=true，不能冒称盘中断档或全天连续性通过。下一轮需隔离剖析预热/计算与事件循环，不增大容忍或删除告警掩盖。
- 无backend业务/前端新改动，本轮实际修改持久启动plist、新建发布审计/保护脚本与文档；复用第28轮同源同环境2683P，未当成本轮新测试。候选/新备份/旧发布/私密配置和冻结原复盘库最终哈希封存。正在使用logs/.../final-env-round28绝对路径不得当普通日志清理；通用重启脚本未改且不作为发布入口。
- 主文档docs/controlled-release-20260915.md，证据outputs/repair_validation_20260914_round29/final_manifest.json。已区分实际部署和源码验收；目标仍active，尚需上述启动遗漏修复、逐股首次可用外部上下文与三模块引用、完整决策链、历史10/193对账、同预算时间外路线/容量/退出研究及真实交易日前向采证。不自动调权/晋级、不放宽买入阈值、不下单、不重写历史。






# Claw 鹰爪 — 工作记忆（精简版 2026-09-18）

## 最新运行入口：2026-10-02 盘后研究专用部署
- 用户批准非交易日维护窗口，17:05原8000 launchd PID80654→15132，57项作业（原54保留，仅上线三项既有研究采证）；腾讯优先/新浪兜底，研究038已迁移，039/040及人工意图失效/撮合链未上线。原交易模块保持9/30发布基线，旧长期目标不恢复、不连实盘。
- **当前从冻结发布包加载，不直接加载工作树**。[启动器](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/launch_release.py>)和[发布单元清单](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate/source_manifest.json>)是运行依赖，不能按普通outputs清理或移动。不要用通用dev_services restart绕过隔离，也不要盲目恢复原工作树启动命令；后续发布需显式合并已批准改动。
- 确切发布包研究回归189通过（未部署THS尾部/撮合断言范围明确排除）；原五组完整账务指纹/业务配置不变、原schema仅添038六对象，未整库复制或历史回填。原candidate_shadow worker及关闭失败仍为已知异常；自然采证预计10/8、翌交易日08:00预计10/9，未新建提醒或长期任务。[完整部署记录](</Users/youzix/WorkBuddy/Claw/docs/after-hours-research-deployment-20261002.md>)。

## 1. 项目概览
- A股多因子量化系统。**Python FastAPI + Vue3/Vite6/ElementPlus/ECharts + SQLite(开发)/PgSQL(生产) + Alembic**。
- 规模：后端 242 个 py（13.8万行）、81 张表、26 组 API 路由、42 个调度任务、259 个测试文件；前端 19 页面 + 17 组件。
- 运行：后端 `127.0.0.1:8000`（受控启动，勿用脚本强杀/清端口）、前端 Vite dev `5173`（HMR 免重启）。改后端业务代码后必须重启才生效。
- 交易边界：主板 ✅ 可交易；创业板 300/301、科创板 688、北交所 👁️ 仅观察；ST/退市/停牌 🚫 屏蔽。模拟盘一律标注"本地模拟/不连接真实券商"。
- 代码规则见 `AGENTS.md`（必读）：改前先读现有实现、最小改动、不新增平行逻辑、不回滚他人并行改动。

## 研究存储约束（2026-09-24用户纠偏）
- 用户明确禁止复盘反复堆积几十GB整库副本。默认复用同版本冻结基线、按需只读导出；确需写入测试的工作库必须独立临时目录，关闭连接后在成功/异常/取消路径清理，不默认永久保存。
- 9/24已删除4回放库、2迁移工作库、1中止部分备份及侧文件，共16个；逻辑184.77GB、实际观测释放75.04GB，保留全部结果/失败报告和必要原始/恢复库。清单outputs/disk_cleanup_20260924/，不要按旧报告自动重建已退役大副本。
- 不删除运行claw.db及其WAL/SHM、不自动清理未知文件；APFS显示容量不能冒充真实可释放空间。规则docs/research-storage-policy.md；尚未实现自动轮转。当前影子记录9/24约14GB另有持续增长问题，未删，后续治理须保持页面读取/失败全分母。

## 2. 代码地图
- **后端** `backend/app/`：`api/v1/`（26 组路由，巨型文件 tenbagger 18.7k / promotion 18.2k / paper 12k 行，只能局部改）、`data/`（scheduler 6.8k 行 + sources/collectors/quote_round/main_fund/auction_evidence/price_chain）、`signal/`（anomaly_scanner 6.4k、bull_score、tenbagger_model、next_day_plan、chip_concentration、dragon_head、b1_signal、launch_precursors）、`promotion/`（晋级预测：modeling/shadow/deployment/ledger/route_contract/versioning）、`paper/`（模拟盘 + 大量 research/experiment 模块）、`trading/`（service/broker/paper_*）、`risk/`（engine/rules/circuit_breaker/lockup）、`sector/`（lifecycle/main_line/leader_tracker）、`factors/`（10类48因子）、`review/`（每日复盘 + GPT 报告）、`core/`（data_date/trade_calendar/stock_tagger/price_limit_rules/data_quality）、`ai/`、`news/`、`push/`、`dashboard2/`、`backtest/`、`strategy/`。
- **前端** `frontend/src/views/`：dashboard / sectors(+v2) / tenbagger / promotion / paper / daily-review / auction / sentiment / news / risk / factors / performance / backtest / model-lab / governance / margin / commodity-linkage / stocks(Detail)。风格深色 #0a0e17，红涨绿跌。
- **调度** `data/scheduler.py` 42 任务：盘前 pre_market、竞价 auction_collect_0920/0924/0925、盘中 intraday_fast(10s)/intraday_slow(腾讯资金30s)/indices(60s)、收盘 close_snapshot_finalize、盘后 after_market/deep_review/daily_review/gpt_review_report、ths_kline_daily、spot_to_kline_19/21、paper_auto_trade_intraday/close、shadow settle 等。
- **脚本** `scripts/`：dev_services.sh、check_env_drift.py、replay_*（回放学）、validate_strategy_edge.py、route_expectancy_readout.py、positive_expectancy_subset_scan.py 等。

## 3. 数据源与字段口径（高价值踩坑，勿凭经验改）
- 七源：腾讯 qt.gtimg.cn（实时 88 字段→stock_spot）、同花顺 d.10jqka（日K前复权→stock_kline）、AkShare/同花顺（板块+资金流）、东财（涨停/跌停/炸板/龙虎榜/竞价 f124）、pywencai（行业257+概念389映射，必须 `loop=True`）、申万（413 三级行业）、新浪（概念/行业成分股）。
- **腾讯已确认**：[3]价 [4]昨收 [5]开 [6]量 [32]涨幅% [33]高 [34]低 [38]换手% [39]PE_TTM [44]流通市值 [45]总市值 [46]PB [47]涨停 [48]跌停 [49]量比 [51]VWAP [64]股息率 [72]总股本 [73]流通股本 [74]委比 [79]净利增速。
- **已纠正的错误映射**：[41]最高 [42]最低 [43]振幅 [44]流通市值 [45]总市值 [47]涨停价 [48]跌停价 [51]VWAP [74]委比。
- **[50] 是五档委差（手），不是主力资金**（5218/5218 行验证）。实时主力资金来自腾讯 hsfundtab（source=tencent, version=tencent_hsfundtab_v1），不得用 field50 乘价伪造。
- **[62] 是长周期涨跌值，不是 5 分钟涨跌**；5 分钟动能必须用连续 spot 快照算。
- 市值单位：DB `circ_market_cap` 存**亿**；腾讯返回元，入库 /1e8。
- `resolve_latest_trade_date()`：所有 trade_date 查询禁止 `date.today()`。
- 收盘真值：`_spot_to_kline_fill(finalize_close=True)` 要求 `updated_at >= 15:00`；只接受 `ths` 或 15:00 后终场 spot 佐证的 `tencent_close`；缺数据不得补 0 伪装。
- 情绪 `sentiment_score` 固定 0~100（当前 `breadth_index_quality_v3`）；买入遇 missing/degraded/stale 必须失败关闭。

## 4. 交易链路现状
- **模拟盘五策略账户**（各 5 万）：A default（明日预案+盘中候选）、B promotion（晋级二板，止盈12/止损5，阈值0.25）、C mainline（主线首板，阈值0.40，止盈8/止损2.5）、D auction（竞价高开，样本少）、E tenbagger（连板高标接力：连板4-8板+封板资金≥1亿+炸板≤2+已开板，止盈18/止损6/持仓3日，参数扫描最优解，29笔样本仍小）。
- **Challenger/Shadow 版本隔离**：`strategy_version` 贯穿下单/排队/持仓/成交/日志；版本变化禁止补成交、跨版本禁止加仓。B/C/D/F Champion 自动买入已暂停；B2/C2/D2/F2 需 20 独立交易日 + 100 confirmed + 多项检验才准晋级，**当前 confirmed=0，不得宣称有效**。
- **已验证为负期望、禁止复活**：十倍评分(Tenbagger 5维)与 10-60 日收益负相关（15 组止盈止损全亏）；断板反包 20 日持有均收 -8.61%（幸存者偏差）。相关代码保留为框架并注明已证伪。
- 回撤开仓闸门 `PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE=unlimited`（当前默认，仅保留单笔级风控）。

## 5. 治理铁律（当前最活跃的工作方式）
- **诚实性优先**：不得把假设写成事实，不得用新表替历史造证据，不得为通过验收放宽阈值/改判据，不得把盘前 health200 推断为收益或覆盖。
- 结果必须可追溯：`snapshot_id` / `data_version` / `source` / `source_version` / `observed_at` 绑定；失败保留上一份成功产物。
- 发布走受控流程（备份 SHA 留档 → 迁移 guard → 禁调度验收 → 精确 bootout → 恢复），见 `docs/controlled-release-20260915.md`。
- 证据不足时降级为"市场证据不足"，而不是编一个结论。

## 6. 历史工作索引（详见 git log 与 docs/）
- 2026-04：牛股雷达 v2、牛股评分 BullScore 7维 + 四轮评审（P0×8/P1×21/P2×4）、筹码集中度、明日预案引擎、板块营地踩坑。
- 2026-08：五策略并行模拟盘、回放引擎与参数寻优、策略E重建（十倍评分→连板高标接力）、断板反包回测（否決）。
- 2026-09-01~03：晋级预测真值/召回/校准修复、每日复盘决策台 v2、收盘真值与情绪质量、C3 首板前向采证（只采证不撮合）。
- 2026-09-09~18：主力资金源替换（腾讯 hsfundtab）、环境漂移、测试网络隔离、受控发布第29轮、收盘快照分母可诊断化、D 路由竞价双来源（东财 f124 + 腾讯）、正期望路线证伪与引擎结算口径更正。**仍在并行推进中**（outputs/today_review_20260917、repair_validation_round*）。

## 7. 常用命令
- 后端测试：`cd backend && python -m pytest tests/ -q`（改核心逻辑必跑对应分组）。
- 前端：`cd frontend && npm run build`（改页面必跑）。
- 环境漂移检查：`python scripts/check_env_drift.py`。
- 服务重启：`bash scripts/dev_services.sh restart`（后端）；前端 HMR 自动生效。

## 8. 2026-09-21 盘后修复验收
- 16:58原后端supervisor平稳SIGTERM重启，PID79104→94606。正式预测质量审计改必要列投影、显式批次身份不再反复解码JSON；与名称/拒绝文案补丁一起部署，不改交易阈值/额度/候选授权、不迁移、不补单/旧批次。
- 隔离回归2032通过、1指定历史fixture跳过；冻结库完整1644条候选/概率/排名/授权/特征/原因前后相同。无profiler提交95.627→78.750秒，仅盘后隔离性能证据；后续20:00真实run353已完成1644候选、总耗时72.271秒，见`docs/postmarket-validation-20260921.md`；次日盘中负载仍待前向验证。
- 线上只读risk/check验证万科全角名称误冲突消除，人工观察/创业板/未知身份仍BLOCK，execution_authorized始终false；订单/成交/现金/历史run未变。详见 `docs/prediction-publish-repair-20260921.md`。
- 首板/额度/确认时延仅离线研究，见 `docs/prediction-followup-study-20260921.md`：C3采证不等于执行授权，下午6信号3涨2跌1平不能推出统一留额度更优；消费长尾区分09:35前等待与后续延迟，T+1尾部风险仍保留。

## 9. 2026-09-21 晚间watchdog修复与含华研究
- 20:49:57原监督进程平稳重启PID94606→5683；watchdog补齐A2/B2/C2/D2/F2原执行入口、同轮去重、风险失败仅采证。生产仅改scheduler单一局部函数，原交易参数/风控/版本不变；不是放宽买点或补历史单。
- 55文件2087通过、1指定冻结夹具缺失跳过；20:51:03核验账户现金、受检订单/成交/持仓/正式run字段摘要不变。仍有盘后intraday_fast missed6.077秒告警，未冒称全绿/次日盘中接管已验证。详见`docs/paper-watchdog-repair-20260921.md`。
- 原106涨停池含华31只、26当前准入，0执行buy_signal/当日买单，但新华医疗C3纯研究confirmed、华通线缆A2confirmed后复验终止；深华发已有C3结构观察。8只具逐股执行日志、23只缺此证据，禁止硬套汇总门禁或watchdog因果。完整31×12及299只含华对照见`docs/hua-limitup-audit-20260921.md`。
- 飞书入队故障/小时限频过期、A2旧预算说明文案仍未在本次修复；不要把watchdog已修扩大成全部链路无缺陷。

## 10. 2026-09-21 可交易化第一轮（21:43验收）
- 21:42:51原服务平稳重启PID5683→9190；修A2未知字段假终态/连续恢复卡死，A2/B2/C2/D2/F2入口与挂单共用结构化复验、已知失效优先于可恢复缺字段/卖盘等待，提交risk_blocked不再假称重试；只撤未成交余量，不反做成交。
- A2默认shadow版本升momentum_retest_v4；共享route_confirmation_v1进入仅5个connected账户执行身份，旧仓按原归因保护性退出，禁止跨版本加仓/补旧事件。资金比例、TTL/风控阈值不调宽，C3仅补原成员帧gate_issues（24000纯函数对照等价）不接交易，A2过期10%预算说明已纠正。
- 最终72文件2685通过/1指定9/14历史冻结夹具缺失跳过，新增3文件128项全通过；21:43:57原服务13项发布检查通过，181交易/364fill/2341订单核心/88持仓/353正式run受检摘要及12户现金不变。仍有intraday_fast missed3.650秒，不能称盘中消费全稳。
- 全12户/53信号/10成交、C3 2265结构/1135eligible/396确认以及567458原成员帧全分母保留；强势推进/整理突破/水下修复有失败与unknown，完整T+1可成交反事实证据不足，不拿终场涨停倒推应买。详见`docs/tradability-optimization-20260921.md`、`docs/tradability-research-20260921.md`。
- 飞书独立入队补偿和限频过期仍未修，多日盈利与下一交易日真实时效未验收；§4旧“暂停/confirmed=0”属于历史概览，不代表9/21事实。不要把工程修复扩大成所有策略已盈利、以后不漏买。

## 11. 2026-09-21 全12户交易执行版更新（22:27验收）
- 用户明确要求A—F及A2—F2全部最新。先验12户已与21:42源码一致，后独立审查发现primary pending缺字段会遮蔽已知VWAP/回撤失效，实际修复而非虚改标签。
- 22:26:15原后端一次SIGTERM，PID9190→11913；仅局部改paper.py行情issues/primary挂单归约与experiment.py七户primary_quote_confirmation_v2身份。A—F及E2旋转，A2/B2/C2/D2/F2保持已最新route_confirmation_v1；不调阈值/TTL/资金，不补旧单、不跨版加仓，旧仓原归因保护性退出。
- 78文件3275通过、1指定历史fixture缺失跳过；新增primary433项通过，版本文件新增30项（全文件55），完整行情原判据2688组一致。22:27:35原服务13检查及12户版本/启用/仅7户旋转3检查通过，原受检账本/现金/持仓不变。
- 仍有intraday_fast missed3.940秒、operational_health=degraded；飞书补偿、C3交易化、单5万元组合配置及盈利验证未混入。详见docs/all-accounts-latest-20260921.md。outputs/all_accounts_latest_20260921/final_verification.json是修复前no-op阶段；最终以deployment_result.json和after_stable_versions.json为准。

## 12. 2026-09-21 盘中可靠性优化（23:36验收）
- 23:34:59原监督服务一次平稳重启PID11913→15535，23:36:34的13项发布检查及6项12户/新契约检查通过，原账本/现金/持仓/挂单/配置/迁移版本不变。A—F及A2—F2保持已最新执行身份，不为性能修复虚转版本；买点、资金比例、风控/TTL不放宽。
- 修复消费者等dispatch锁时预取旧轮次竞态；五connected消费者批量预判原规则已处理事件，其余仍实时查询，保留原排名。401终结事件隔离基准1203→9 SELECT、结果相同，仅子步骤性能证据。加入真实墙钟与阶段耗时日志、/health quote_consumer，不把行情钟或采集完成钟称DB提交。
- 飞书失败/业务待flush入口暂存至原事务关闭后独立session补偿，七primary、五connected、手动owner接hook；避免SAVEPOINT隐式preflush污染交易事务，手动cleanup保留原错误/取消。合批绕过超字节头卡、小批限额下有限等合并，保留过期/冷却原因。不补历史过期信号、不发真实测试推送。session.info仅volatile handoff，独立commit前不可抗崩溃；补偿行标签unknown，不伪造原时点市场状态，非保证送达。
- 最终92文件3577通过/1指定9/14历史fixture缺失跳过。旧号段护栏被运行库隔离拒绝的一次红结果保留；改用只读冻结全部5592代码并锁SHA，不跳过断言。以outputs/intraday_reliability_20260921/release_final_v3.*及deployment_result.json为准。
- 单5万元研究复用原capacity CLI，加共享上限对照而非完整组合回测，保留selected/displaced/failed；真实全53条（10成交/3撤单/1不足一手/37容量/2风控）不删，统一期初组合与反事实风控证据缺失，净收益/winner为空。未合并12个独立5万元账户、未自动加仓/调权/晋升C3。
- 发布后仍intraday_fast missed1.859秒、operational_health=degraded；quote_consumer盘后not_run，不能称盘中稳定或收益已改善。飞书worker运行、last_error=null、46sent/7expired未变，新补偿统计空。详见docs/intraday-reliability-optimization-20260921.md。

## 13. 2026-09-22 调度扫描公平性修复（00:19验收）
- 历史23:35的1.859秒missed与启动全市场K线预热重叠。只改tenbagger.py的`_load_main_wave_plan_candidates`：原9列/排序全量读取改2048行流式分片，全市场及二阶段每16股主动让出；读取失败/取消与close双故障保留原异常。候选/评分/风控/资金/TTL/misfire_grace不变，不靠放宽告警阈值。
- 固定9/21 23:35研究钟及既有16:45只读冻结库；uvloop三次最大心跳延迟5.703–6.149秒→0.255–0.263秒，完整80候选逐字节一致，9条SQL文本/顺序一致。独立APScheduler无交易回调旧missed、新正常执行迟到45.1ms。是同类阻塞机制复现，未采到历史现场栈，不能断言唯一根因或把心跳延迟说成买单延迟/扫描总耗时。
- 最终98文件3900通过、1指定9/14历史冻结夹具缺失跳过；新增12项覆盖协作、流式关闭、真实取消及双故障。完整证据outputs/scheduler_blocking_20260921/release_final_v2.*；中间主动停止的全组不算通过。
- 00:15:36原监督服务一次平稳更新PID15535→17526，00:19:10的13+6项发布检查全通过；12户全部启用且身份不变，原受检账本/现金/持仓/挂单/配置/迁移版本不变。实际预热完成，随后180秒6次健康观察捕获6个不同快频执行时钟，无新增missed/error/max_instances；历史告警保留。
- 仅已验证工程修复及非交易时段窗口，不承诺全部盘中负载永不漏买、不代表收益/飞书真实送达已验证。统一5万元未转组合执行，不是只等明日拿真金试验。详见docs/scheduler-fairness-repair-20260922.md。

## 14. 2026-09-22盘中前向核查（日志截至10:31:26）
- 只读审计，未改生产/阈值/资金、未重启/补单/发测试推送。PID仍17526，242个app源码与昨夜基线SHA一致。不能将00:19的180秒无告警扩大成今天稳定；health operational_health仍degraded。
- 09:30–10:31:26全日志结构化告警83 missed、13 max_instances、9 business_degraded；快频missed36次（最大观测迟到6.522秒）、watchdog missed8次。09:20:06关键竞价窗另missed4.606秒；采集DNS失败和历史证据缺失不能事后补造。
- 115条结束消费中外层112completed/3failed；发布→开始P95=16.367秒，整轮派发P95=46.135秒、最大86.801秒，均非买单成交时延。SQLite锁冲突进入持仓风控与次账户日志写入；_process_quote_round_shadow吞掉部分异常后外层仍completed，watchdog依赖其完成钟，残余错误传播缺口已确认，尚未修。
- 飞书入口补偿真实触发：按<=10:31业务钟筛选40buy_signal，11入口recovered；末通知状态37sent/1expired/2throttled，sent非用户已读证明。数据库采样在10:34:20且短查询各自快照，不是10:31完整PIT冻结，统计不得直接充当回测。
- 10:00正式窗口9次timeout，run358–366皆generation_pending无completed；10:30上下文run368于10:26:51完成571候选/gate_passed=1。B/C主账户各63条prediction_batch_blocked，供给确实受影响但不能指定某股因此漏买。12户有扫描，19委托/13成交，其中买入成交5笔（主账户A：2笔；次账户B2：2笔；次账户C2：1笔）。
- 统一5万元仍未转组合模拟执行；本轮结论为部分修复生效、整体未通过盘中稳定性验收。详见docs/intraday-repair-verification-20260922.md及outputs/intraday_verify_20260922_1030/。

## 15. 2026-09-22盘中残余故障修复（午休12:09验收）
- 12:07:24原8000监督服务一次SIGTERM，PID17526→26969；12:09:47完成11项交接/账务及6项功能检查。12户全启用/策略身份不虚转；原受检账本、现金、持仓、3笔待处理订单、配置/迁移版本均不变，12户现金勾稽差额0、六项异常0。未补历史订单或发真实测试飞书。
- scheduler主/次显式失败回执、成功钟只在全阶段确认后推进；新失败不借旧成功去抖但保留成功ID去重。A2 drain失败保留原inflight顺序且报告失败，连接阶段继续其他户；去重/降级/兜底同样检查。取消与uncertain标记不会被rollback/close二次异常抹掉而误复验。
- A2/B2/C2/D2/F2逐户新Session、仅确定SQLite busy/locked最多一次新会话复验；原TTL/决策时钟不刷新。Challenger干净阶段结束陈旧读快照再BEGIN IMMEDIATE，保留原排名与提交后订单幂等；E2仍在原主循环。新闻逐篇真正commit/owner rollback，不用顶层SAVEPOINT提前提交；impact-map冻结原时点5项持仓标量避免rollback后MissingGreenlet。
- promotion只做重叠窗口数值复用、同SQL流式分片、每16代码主动让出。9/21冻结同1697代码特征/顺序一致，K线查询+确认9.614→6.260秒，10ms观察协程最大间隔6.745→0.161秒；1664完整候选及质量门逐字节一致，构建33.532→32.071秒。是单对隔离测量，不是9/22 PIT/正式publish重放，不宣称整链同比提速或解决线上所有质量审计等待。
- 最终113相关文件4169通过/1指定9/14历史fixture缺失跳过/26既有async标记警告，0联网企图，app/tests/scripts与冻结标签库哈希稳定，权威产物outputs/intraday_repair_20260922/release_final_v6.*。中间红测/取消全组/测试隔离串扰均保留，不把v1/v5的被拦联网测试当发布准入。
- 12:08:18启动预热完成，午休120秒4次观察无新增job告警、增量日志无WARNING/ERROR；quote_consumer=not_run，不能外推下午盘中稳定。飞书worker运行、last_error=null，交接45sent/4expired未变，不代表新信号或必达验证。统一5万元仍未接组合执行，风险/仓位/TTL/misfire/推送限额均不放宽；不承诺零漏买或收益改善。详见docs/intraday-repair-release-20260922.md。

## 16. 2026-09-22下午开盘核查（日志截至13:15:12）
- 只读前向验收，未重启/补单/发测试推送/改生产或额度。PID26969，242个app文件与午休release_final_v6一致；13:24保护核对PID/源码/配置仍未变，不扩展交易验收窗口。原8000线上模拟盘，不是券商实盘。
- 1305合法窗口13:00已触发，run369屏障后run370于13:02:06完成890候选、8actionable、gate通过，外层13:02:11完成总130.477秒；不能说还未触发或仍超时，也不能认证120秒SLA/全面提速。B/C发布阻断随后解除；B执行交集仍0，C有候选到VWAP门；D仍缺开盘run357的auction_data，不能靠1305补造。
- 12户下午有扫描，新增4买点全部飞书sent、3批渠道成功，未新增委托/成交。A昂立教育/欢瑞世纪/金域医学被真实日开2只限额挡住（A现金42681.96）；C2黑猫股份被日开数或持仓数上限挡住。是有信号后容量门阻断，不是未识别；统一单5万元组合执行仍未接，工程修复未解决资金使用政策。
- 4信号审计行墙钟→渠道返回2.065–5.628秒，报价/决策轮次钟→渠道返回11.408–41.826秒，均非DB提交或用户收到时延；今日49sent/4expired，12入口补偿均上午，不冒充新修复触发。
- 消费结束28轮=27completed+1degraded，0failed；另1轮在途、1轮latest-only合并无独立终态。五连接次户各27成功回执/attempts1，显式SQLite locked/busy和失败/重试均0；只是未复发样本，异常恢复未自然触发。watchdog30次只有9最近完成跳过+19忙跳过+2安全清理，无完整交易接管。正常轮发布→开始P95 15.334秒、整轮派发P95 48.557秒/最大75.987秒，非单笔成交延迟；长尾仍在，不能用不同负载的上午样本推导改善因果。
- 13:00:58再现两任务missed约1.917秒，13:13快频max_instances一次，共2missed/1max_instances/2business_degraded，health仍degraded；不能说1.859秒同类告警已根治。13:00腾讯覆盖降级失败关闭，上游概念资金/新闻仍有独立异常。
- 12户现金勾稽0、六类账务异常0；3笔上午卖单仍因原限价内五档深度不足等待，未伪造/取消。细证据与边界见docs/intraday-afternoon-verification-20260922.md、outputs/intraday_verify_20260922_afternoon/。

## 2026-09-23 用户纠偏与独立账户修复（最新，不覆盖历史）
- 用户明确不要共享组合，目标是ABCDEF主次12户各自独立初始5万元。22:35原launchd已将PAPER_PORTFOLIO_ENABLED=false，停止共享新增候选/买入/通知，原共享5旧仓仅保留正常保护退出，不迁移、不删账。前端共享Tab移除、C3入口仅C；不要重建共享账户替代资金利用问题。
- 23:03原8000服务PID90438已发布primary七户日/题材配额原单归组与C3发送前最终复验。3app+3test、711源冻结、51文件2104项隔离通过；独审/发布门均通过。14组账本前后SHA一致，12户实际版本匹配、初始资金均50000、13活跃账本现金差额0。原风险/比例/TTL没调宽，7primary仅新增配额身份合同，5connected版本不变。
- 配额只合并同一可验证原委托的分片，未知/冲突不释放名额；不删原成交日志。C3在attempting等待后新session复验原当前池和StockTag，释放读事务再验原TTL/池expiry；明确失效拒绝、未知仅原TTL内等待，原确认价/时钟不改，仍无执行账户。
- 旭光电子属A午后二次回收，中晶科技属F2急拉后持续确认，均不是C3；C3已发12条终场8负3正1平只是价格标记，不是T+1收益。原12户45条已发中35条C2，不能混作所有路线样本。形态早观察/首次回踩及更高资金利用政策仍需多日PIT验证，未上线新阈值或承诺启动前买点。
- 今日9个原买单均单fill足量，分片名额潜伏错误不是当日闲置的实证原因；现金闲置还来自单股目标×槽数、整手和跨版本补仓限制，不能声称修配额就解决收益。盘后发布与账本验收不代表自然前向时效/退出/收益已验证。
- 交付：docs/independent-account-correction-20260923.md、docs/independent-account-boundary-repair-20260923.md；证据outputs/independent_accounts_20260923/final_check/（release_result与delivery_verification均passed；natural_forward_accepted=false）。

## 2026-09-24 同批候选只观察实验已部署（07:02）
- 用户确认原12户照常、实验只观察后，原8000 launchd一次SIGTERM从PID96451→14764。新旁路复用原候选/报价/实际门，写Claw根outputs/strategy_candidate_shadow不可变研究文件，不写业务确认事件、不下单、不占预算、不发送实验飞书。没有恢复共享组合；C3仍无执行账户。
- 模拟盘→本族→策略对比，显式日期手动刷新：A/B/D/E/F主次各两路，C/C2/C3三路；无新增全局C3/共享Tab。5/15/30分钟仅参考标签，非成交净收益，本期无收盘/T+标签；运行中不等于有自然样本。
- 源freeze v2共722文件，digest899929ac0fe28e8fb5feb9beb5c4ff2f5a34570ebc817d978dccd67d6ccced5f；原sealed核心c77d0dff…beaf47不改。主47文件1968/腾讯10文件117/guard40全绿，独立316回归及19确认边界例。独审v1曾发现确认时钟被后续成员资格清除，真实六路线红例保留，v2修复且首次无钟/坏钟/reset/跨流仍unknown。
- 原14保护账本+2当日查询三次48文件hash/行数/bytes相同；12执行版本/各初始5万、现金对账/订单/持仓/正式v6worker不变，旧shared五仓仅保护退出。23发布checks passed；startup准确12只读绑定、研究worker/corematch正常。
- 原5173页面六族13真实只读GET验收通过；其余API隔离空响应避免账户GET估值写入，不冒充全页财务联调。首个验收脚本glob误拦/src/api模块导致失败，保留失败证据，新v2脚本精确/api/v1/**后通过；未因此修改应用或二次部署。
- 同时加载已审Tencent逐响应合法观测钟/尾批修复（普通spot/质量门不变），不是9/23缺量比或D收益问题已解决。B2/C3失败研究假设没有晋级。盘前只有启动记录，自然开盘覆盖/时效/表现尚未验收，禁止承诺收益。
- 交付docs/candidate-shadow-live-20260924.md；冻结/独审/发布/只读浏览器证据outputs/strategy_shadow_live_20260924/；自然清单NATURAL_FORWARD_CHECKLIST.md。

## 2026-09-24 工程追加修复已部署（16:41最新，自然待验收）
- 12:09版下午继续失败：14:30九次真实timeout/run431–439全blocked/0候选；14:50累计46missed/11max_instances，218完成消费median20.445/P9543.169/max70.949秒，不能掩盖。D原多帧缺口不补造。
- 16:41原8000 launchd受控追加v4，PID25133→35134。相对已部署v2仅promotion近期记忆stream/yield及burst调用内清洗复用、strategy_iteration_shadow原scan提私有确认重建helper并128分片/close-first；完整非C3历史/reset最大水位/C3全天first-hit和原SQL/阈值/TTL/版本/仓位均保留。明确接受处理/close失败不发第二只读SQL，仍失败关闭而非逐异常时序等价。
- v4冻结732文件digest aab300a4343aa90bcb820666d065aef9eb03ea246eb56317eb9fa4eb2b8a4da0；83模块3513通过+1精确9/14旧夹具缺失skip，源/fixture稳定、blocked[]。完整1591+79候选76MB两臂全等；51774 C行五状态/SQL全等。文件转运心跳4.143→.04394秒及峰值下降只支持机制，不是SQL/线上SLA；总wall单对略低、CPU未降，不能承诺根治或收益。
- 15:45原3卖单收盘close_unfilled撤单/0fill，非持仓卖出；ID13/position102标记估值+8非成交。新15:54真实reference经独审；fresh16:26六表/pending/cash完全等值、原3账户42servicecalls恢复proof、437原通知失效proof均真跑，不空pending旁路、不回填旧仓/补单。
- 16:38避开原16:30/35任务fresh双可见，16:39父371证据签认，16:40:19一次marker，16:41:08结果23checks全true。baseline/pre/post原14+today3全等，12执行版本/各初始5万/风险/正式v6通知/旧shared2持仓仅退出不变；观察shadow生产权限false。
- 89c发布后独审371required+4supporting、235native、732源、51份JSONL全部核验一致；首次重启health URLError后16:40:33新进程健康，故非全程零错误。权威outputs/intraday_repair_20260924/release_v4/release_result.json；新旧release marker均已消耗，禁止复用重启。v3准备从未上线，失败/证据保留。natural_forward_accepted=false，下一20:00候选及下一交易日竞价/盘中按FORWARD_ACCEPTANCE_V4继续只读验收；已上线不等于永不漏买/收益改善。

## 2026-09-24 盘中工程修复已部署（12:09，14:09前向仍失败）
- 14:09最新observed窗口出现21 missed/3 max_instances；137completed消费median15.262/P9531.931/max48.840秒，不能用13:10的零告警代表全下午。promotion_1400 postprocessing_timeout120.287秒，但RO实证run430 completed971候选/17ranked/5上游actionable，B/C13:57:16引用该run。不是全部未发布；自然时效仍不达标，不复用12:09已消耗发布marker。
- 上午自然日志仍有候选超时、missed/max_instances和消费长尾。六处最小等价修复：竞价仅对合法可推导行查历史量；竞价await后单飞复查/finally异常及取消保护；历史质量审计流式让出；promotion学习SQL排除原必弃盘中行、首板全历史stream/yield；交易轮次投影调用内日历缓存；次账户原幂等阳性短路重复查询。不改原公式/排序/TTL/买点/仓位/风险。
- freeze_v2 726文件digest c52f777c6d73f18455ffe551aea27902ae0afc8259bafc2414e5ea146affa325；80文件3296pass+1原9/14夹具缺失skip，68新增边界通过。完整1591+79候选两输出字节一致，但离线总耗时36.328→37.475秒未改善；局部心跳改善不等于整链SLA。
- 11:48采证因当天日志32MiB截断被拒，无重启，attempt_1148保留。补同快照分页后12:06完整auto22531/约68.5MB、通知225/C3当日确认0及14保护组；新E2排队单已过TTL但午休仍submitted，原service隔离恢复按原过期取消，不在线手改。shared已有3自然退出、2旧仓，仅退出功能不恢复新增。
- 12:08父218证据新签认，12:09原8000 launchd一次SIGTERM，PID14764→25133，12:09:29完成23项检查；baseline/pre/post原14+当天3完整集合/字节/行SHA一致。12户原执行版本/各50000初始/现金差额0、订单持仓及4pending、通知v6/worker、影子只观察均保留。
- 发布结果outputs/intraday_repair_20260924/release/release_result.json；单次marker已消耗，不得复用发布脚本重启。89c发布后独审218签认/51组字节通过。natural_forward_accepted=false，不承诺零漏买或收益。详见docs/intraday-repair-20260924.md。
- 13:10首段自然日志（6ff独审）：promotion_1305原13:00预热、13:02完成116.9215秒；20completed消费median11.705/P9520.080/max48.840秒、另1running，matched missed/max_instances0但4K线降级及news90秒timeout仍在。不同负载不能据median认定因果提速。
- 13:13:58单RO账本：run428真实835候选/17ranked/5上游actionable；12户27/28轮都有scan，2新order/0trade/0fill、13cash0。C2二六三受容量后区间失效；F2龙头股份待撮合后持续性失效取消，飞书分别waiting/pending不是成交。旧E2排队单下午按原TTL自然取消。
- D在新批次quality metadata的实际缺口是多帧：latest2986/2986合法、field_degraded=false、stale=false，但至少2不同source_quote_at有效量帧369只（12.36%）低于95%，path_degraded=true；勿凭笼统日志写成2986只全缺字段。14:00等后续批次及次日自然竞价仍待验收；详见outputs/intraday_repair_20260924/NATURAL_FORWARD_1314.md。

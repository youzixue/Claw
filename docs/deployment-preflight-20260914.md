# 原8000服务部署前只读预检（2026-09-14）

## 结论 / 观察范围

**未部署；不建议直接执行现有restart脚本。** 本次仅源码阅读、进程/磁盘观察、原服务`/health`及SQLite `mode=ro`+`PRAGMA query_only=ON`查询。未重启、kill、迁移、下单、业务GET、生产写入或复制生产库。独占输出仅本文；源码与父并行文件均未修改。

观测时间为本机CST/Asia/Shanghai **19:32:45–19:35:40**，不是任务标题推测。距旧任务21:20仍约104分钟，但旧服务20:00已还有多项写任务，不能把21:20当唯一安全截止点。操作前必须再次核实时钟与PID。

## 实际服务与启动控制

- `127.0.0.1:8000`唯一监听PID **57866**，PPID **1**，启动 **15:34:23**。命令Python3.11 `-m uvicorn app.main:app --host 127.0.0.1 --port 8000`，无reload/多worker参数；cwd为项目backend。标准输出/错误均指向`logs/backend-uvicorn.log`（约757MB）。
- `launchctl print gui/501/com.claw.dev.backend`只选取非敏感字段观察：running、pid57866、runs4。实际plist为`logs/launchd/com.claw.dev.backend.plist`，KeepAlive=true、RunAtLoad=true。**只kill进程可能自动重生；必须先处理此后端监督者，不能误停前端。**
- plist命令明确`unset CLAW_DISABLE_SCHEDULER`，保留了`QUOTE_ROUND_CODE_VERSION=src-paperfix-7fae85c9e152c286ead6`及实验/推送覆盖。不能仅在外层shell设置禁调度就认为生效，也不能让新代码沿用旧版本标签。
- `scripts/dev_services.sh:114–180`会限时后kill-9、清端口、重写plist，并在启动中unset禁调度；全局restart还波及前端。重新生成的plist不包含当前额外环境块。因此本次发布不宜盲用此脚本，更不能以它代替备份/迁移计划。未读取或输出凭据环境。
- 19:33 `/health`：status=ok、scheduler.running=true、49 jobs；`ths_kline_recent_repair`下次**当日21:20**、spot兜底21:00、深度数据/指数历史/正式预测20:00。paper_auto_trading=false是当前重入状态，不是禁止自动交易开关。health只证明当前进程内状态，不证明历史运行完整或部署成功。
- 旧THS破坏式实现风险由父已确认；本次确认旧进程仍在且任务已排程。磁盘当前scheduler的THS函数已改为采证，不可据此宣称15:34进程已装入该修复；没有读取旧进程函数内存字节或建立完整历史源码复原。

## 启动副作用与安全边界

| 入口 | 已读取的代码事实 | 部署含义 |
|---|---|---|
| `app/main.py:20–55` | lifespan先init_db再判断CLAW_DISABLE_SCHEDULER；调度启动失败被捕获后仍可继续服务 | scheduler_disabled不等于只读启动；HTTP起来不等于业务健康 |
| `db/session.py:338–359` | WAL、busy_timeout、metadata.create_all、自动补列/索引 | 有DDL/写锁，且不是完整Alembic升级；init_db不能用ro库作为可运行生产启动方案 |
| `db/session.py:118–148` | 若新增nlp_status列，会UPDATE历史finance_news | 本次只读已验证生产已有nlp_status，因此该条件当前不触发；不能对其它旧库泛称启动无回填 |
| `scheduler.start:3625–3661` | 立刻启动prewarm、index_history、预测catchup、paper/推送/quote循环 | 正常启动不是仅注册未来cron；会马上有网络/写入任务 |
| `scheduler:2522–2544`、`index_history_window.py:109–150` | 盘外启动可读历史源、reconcile并commit质量记录 | 若已有合格冻结窗口可already_verified跳过；这不是稳定的无写保证 |
| `index_history.py:121–200` | 补缺历史StockDaily，close无效旧行原位修复，已有好行保留 | 若本轮要求历史完全不写，**此入口仍需父明确保护/隔离**；不能只修stock_kline便声称所有历史不可变 |
| `main.py:56–111` | 调度开启时创建今日K线不足补偿任务 | 关闭调度可同时跳过此任务；正常开启仍须审核当前日投影约束 |
| `scheduler:3845–3855,945–1007` | 5秒后paper watchdog，晨/午交易时段才进入execute=True链路 | 今晚此入口时段门会跳过，但盘中重启不能保证不下单；quote消费亦会运行 |
| `paper.py:479–529,8641–8667` | run_paper_auto_trade先get_or_create账户再做自己的时段判断；缺账户会flush/commit并可能建NAV | main/init_db本身未直接创建账户；进入业务消费者或业务GET则可能创建，execute=False也不代表全程只读 |
| `scheduler:146–168` | 当前catchup盘后从名义时点且只限原窗口 | 19:35不补1510；20:00–21:30可尝试2000，过期不得补造。停服造成缺批须据实诊断 |

### 必要源码/运维保护建议（未实施）

1. 父确认是否允许启动时历史指数补缺/原位修复；如不允许，先让此路径与历史K一致隔离采证，或保持全调度禁用。不能误称旧行修复是本次先验数据。
2. 部署需可保留禁调度环境的后端专用启动入口；现有脚本显式unset是阻碍。若需真正无写smoke，另需明确跳过init_db或在隔离库验证，不能假设CLAW_DISABLE_SCHEDULER关闭DDL。
3. 把预期schema/append-only触发器校验作为放行条件；不要靠create_all或health ok默许部分升级。保持交易风控与原正式窗口，不通过业务调用“热身”。

## SQLite容量 / schema真实状态

只读连接使用`sqlite3.connect('file:/Users/youzix/WorkBuddy/Claw/backend/claw.db?mode=ro', uri=True, timeout=3)`，立即设置并断言query_only=1；只执行SELECT、table_info及读取journal_mode。未执行checkpoint、VACUUM、锁探测写事务或integrity_check全库长扫描。

- 主库**12,995,723,264 bytes**（约13.0GB十进制/12.10GiB），WAL **65,586,312 bytes**、SHM131072字节；在线继续变化。
- 所在卷可用**87GiB**；现有9/2备份约8.1GiB，是过时备份，不能替代这次一致性备份。
- 可容纳约12.2GiB冷备+12.2GiB演练副本，理论尚余约62GiB；建议额外预留至少一主库规模加WAL增长/迁移临时空间，不把df余额当复制耗时、IO性能或完整性已验证。未实际测速/备份。
- journal_mode=wal；当前alembic_version为**027_fund_order_breakdown**。
- factor_evaluation_run表/unique/index存在；两news版本表及声明列/索引已存在，**四个news no_update/no_delete触发器均不存在**；stock_kline_observation表不存在。
- lsof抽样数据库持有者只有57866；这是瞬时打开文件观察，不能证明下一秒无脚本写者、无锁或wal已checkpoint。生产并行写入仍活跃。

### 028/029/030的实际含义

- 028只在表不存在时建factor_evaluation_run与索引，存在便return；不回填旧评估、不验证已有表结构完全正确。
- 029按需建news两表/索引，并独立创建SQLite UPDATE/DELETE拒绝触发器，不回填旧到达时钟。本库已存在表不代表迁移完成：**仍缺触发器且版本仍027**。
- 030建stock_kline_observation/唯一键/索引、UPDATE/DELETE拒绝触发器；不改历史股价。downgrade直接raise要求证据保留计划。
- 028/029降级会删证据表；不可作为常规回滚。030明确不能盲降级。**优先兼容schema的代码回滚，新增证据保留。**
- `alembic/env.py:15–17`以settings.DATABASE_URL覆盖ini；迁移演练必须显式指向副本绝对URL，不能只改alembic.ini然后误写生产。不得stamp head冒充真实迁移。迁移按链027→028→029→030，经副本验证后执行精确目标，不盲追并行新增head。

## 给父的最小操作序列（仅计划，须由获授权部署者执行）

1. **冻结发布单元**：等所有agent交付，归档完整本轮源码/依赖/测试和当前plist（私密配置不入公开报告），记录明确source hash。当前工作树大量untracked，不能用git HEAD/reset当可靠回滚包；也不能把当前磁盘文件误当旧PID的源码。确认回滚可用包，否则保持维护，不贸然上线。
2. **选择停写时间**：在旧21:20 destructive任务之前留足停服/冷备时间，最好避免20:00任务刚开始时切换；若准备不足，应由授权者选择提前停旧服务保持维护，而不是为保可用让已知危险任务继续。不在本任务中代为停止。
3. **先停后端监督者，再确认写者退出**：识别当前launchd label/PID，使用其停用/卸载机制防KeepAlive重生；等待graceful shutdown和DB句柄释放，重复核查原8000无监听、所有外部写作业已停。不要直接kill-9或运行全服务restart；退出卡住需明确决策，不能把进程消失当事务/备份完成。
4. **冷一致性备份先于任何迁移/新启动**：所有写者确已退出后，把主库及仍残留WAL作为同一冻结集合保存（SHM可一并封存但不能视为权威数据）；记录时间/大小/hash。不要在活跃WAL库上只cp主库，更不能删除WAL“解锁”。如果选择在线SQLite backup API，先在演练验证，控制长读/WAL增长并确认成功；本次未实施，不把在线裸文件副本叫一致备份。
5. **在副本验证恢复与迁移**：原备份保持只读，另建演练副本，确认可打开/quick_check或充分完整性检查与关键计数，再在副本执行明确028–030升级；核对revision、表列/唯一键/六个新触发器（news4+kline2），验证禁止UPDATE/DELETE仅在副本中测试，确认历史核心表计数/内容未改变。不要直接复制待恢复WAL到已运行目标旁。
6. **生产迁移**：再次确认无写者，备份有效且可回滚，在原路径执行已演练的精确030升级；读回schema与revision。SQLite单写者和DDL争锁必须尊重；busy_timeout不是并发迁移安全许可。失败则保持停服，先检查实际部分schema，不先stamp或立即启旧调度。
7. **原8000受控smoke**：仅新后端单实例，绝对Python3.11/正确backend cwd，显式新版本标记，先保留CLAW_DISABLE_SCHEDULER=1且不能经会unset它的原脚本；注意init_db仍会运行，需此前副本验证。只访问已查无副作用`/`、`/health`，确认新PID/启动时间/原端口/调度关闭。禁止候选、paper账户等业务GET热身。
8. **单独放行调度**：只有历史写保护、迁移、纸盘/推送准入与回滚条件均确认后，才受控在同8000启动最终调度实例；不能同时留两个写者。验证新运行版本/任务排程/逐路线诊断。2000若错过窗口则保留真实缺批，不回填1510或追造已过期2000。首次真实业务运行属于上线后证据，不属于盘中历史验证。
9. **失败回滚**：先停新监督者/进程并保护迁移后新增证据及库/WAL；优先用经验证兼容030的旧/安全代码配合禁调度恢复原8000。若一定要恢复数据库，保存失败后的完整库证据后，离线用同一备份集合恢复，禁止旧主库混新WAL；这会丢失切换后写入，需单独授权和证据保留。绝不能直接alembic downgrade删新闻/因子证据，更不能重新启用已知破坏式THS旧任务。

## 验证与限制

本次所有已执行系统/SQLite命令成功；未启动后台任务。外部SQLite官方备份资料web_search请求55秒超时，未取得检索结果，故未伪造外部引用或宣称完成文档认证；备份方案在执行前仍需副本恢复实测。未跑pytest（无生产代码改动），未运行启动smoke/迁移/备份性能测试，未证明真实旧进程内存源码或锁空闲。本文是部署前条件与操作建议，不是部署验收报告。交付后冻结，不扩大范围。

## Round4限定源码复核：完整调度放行条件（后续追加）

此节是父新授权的只读源码复核，**覆盖前文旧index问题的当前判断**，不是新服务上线证明。没有查询/操作生产库或服务，没有改源码、运行测试或新代理。行号以本次读到的并行工作树为准；测试及最终部署由父确认。

### 已修复，不再列为当前历史覆写问题

- `index_history.py:121–219`现为`audit_only_no_historical_projection`，StockDaily只读，候选统计不insert/repair；`index_history_window.py:129–158`追加本次DataSourceHealth窗口及projection_audit，inserted/repaired固定0。启动与每半小时历史窗口仍会网络采集/追加新观测，但**不是旧StockDaily原位修复**。前文对应风险是旧版观察，不能继续当新版阻断。
- StockKline：`scheduler._batch_upsert:1454–1455`拒绝通用写；21:20 `_ths_kline_recent_repair:5085–5180`把过去21日候选送入统一writer，不再delete/upsert旧K。`kline_observations.py:144–175`将过去/未来/未确认日隔离，仅now当天且日历确认投影；旧投影改动前保存原字节、available_at=NULL。当日同股刷新及新历史候选追加不等于覆盖历史K。030触发器和新源码实际生效仍由父部署验收。

### 本轮发现的明确剩余阻断：60秒指数fallback

`_intraday_indices_and_sentiment`（scheduler:2555起）在pre_auction/morning/afternoon/after_hours可进入。实时指数失败时，2616–2633调用`get_index_daily`并取`date<=today`最后一行，**允许非当天日期**；2667–2672随后用`_batch_upsert(StockDaily)`更新OHLC/昨收/涨幅。2674的`_current_index_snapshot_records(today)`只在写入后用于质量计数，不能撤销历史写入；1454的通用保护只针对StockKline。

因此修复reconcile仍不足以允许宣称历史StockDaily安全。父开完整调度前须让此fallback只允许经确认当天投影，旧日只能隔离采证/明确跳过，并测试“实时失败+日线只到昨日”不改旧行。建议共享StockDaily写入口也防非当天记录，避免只靠上游筛选。此发现已即时报告父；如父随后修复，应以新源码及测试覆盖更新状态，不把本文行号当未变事实。

### 其它入口：不可变证据与正常投影必须分开

| 入口/对象 | 当前源码读到的行为 | 放行含义 |
|---|---|---|
| 20:00 deep_review（3149–3243） | 申万最新行业映射与质量记录upsert/commit，observed_at=现在 | 是当前元数据投影，不是StockDaily/StockKline历史改写；不可据此重构旧板块PIT |
| startup_prewarm（4285起，5537–5635） | 生命周期强制刷新、雷达/龙头/明日预案快照、排名预热，trade_date=None可能解析到最新存储日 | 不是严格只读；可能更新带旧交易日期的可变看板/候选投影。未发现这条预热线直接改五类目标不可变原证据；不能把新snapshot_time当旧日可见。若要求一切旧日期行都冻结，则这些预热也应暂停，而不只是修价格 |
| 新闻原始/AI刷新（news/engine:302–389） | 内容/分析版本add+flush，旧同hash复用，变化追加；当前FinanceNews页面投影可更新，包括旧publish_time新闻 | 旧新闻发布日不等于旧版本覆写；新分析available_at为本次。029 SQL触发器需验收，不能仅依赖ORM拦截。未发现这条刷新更新/删除既有不可变版本 |
| 20:00正式预测/启动catchup | ledger:338–369、477–528按run_key已存在即返回，否则追加；models/promotion:371–381禁止ORM更新/删除 | 正常新增本次attempt不是回写过去run；pending和完成分行保留。catchup不得超原时间窗，不追造1510。未发现已检查正式生产路径改旧run/snapshot |
| 20:00预测附带学习、20:20学习 | promotion:1884–1913评估旧pending兼容PredictionRecord；1855–1881可重置当天提前结算字段 | **确会修改旧预测兼容record的结果字段**，不是不可变run。若保护对象包含兼容结果旧行，必须另禁这条学习；若允许到期结算，需明确其为现在结算的可变结果投影，不冒充旧预测内容 |
| quote restore（3941–3952） | 同日近8分钟archive只恢复anomaly内存 | 没有直接重放订单；不能与下面实际消费者混淆 |
| start即注册的quote/paper消费者 | 4080–4113降级轮也会清理pending买单/追加影子证据；健康轮走持仓风控、execute=True账户、Challenger；watchdog晨午时段门 | 开调度并非仅采行情，订单状态/持仓/账户会正常变化，旧创建日pending订单也可过期；这是订单生命周期，不是已成交历史价格重写。抽查未见旧PaperTradeLog成交记录覆写，但本轮不是全交易栈不可变证明。若当前承诺零下单/零账户变动，必须保持调度关闭或有另经验证的消费者禁用机制 |

### 最终放行清单 / 当前建议

1. 父先完成60秒指数fallback非当天保护与回归，确认已修index audit-only测试通过；本子任务不代修或代测。
2. 在原8000禁调度smoke后，验明版本/030及新闻、K观察触发器，而非只看HTTP200；前文init_db写入与launchd unset风险仍适用。
3. 明确允许范围：允许**当日价格投影、当前元数据upsert、新观察版本、新正式attempt、到期结果评估**，不等于允许旧价格/原预测/新闻版本/已成交原始凭证重写。完整调度包含真实纸盘生命周期，需父单独确认授权。
4. 在上述闭环前，**建议保持禁调度维护，不给全调度安全背书**。本轮找到确定StockDaily入口即可阻止放行；有限源码复核不能证明所有未来分支、跨日长任务或全部交易栈绝无历史副作用。父修复后需重新核对具体阻断，不无限扩展本子任务。

### Round4父修复后定点复核：上述StockDaily阻断已解除（源码层）

已重新读取scheduler当前源码：2639–2641显式跳过非today日线；2675在写入前调用`_current_index_snapshot_records`；2683–2695对0/3亦record_failure；1456–1460的共享StockDaily upsert入口重新按实际`date.today()`拒绝非当日记录，覆盖网络等待后进入写入口已跨日的情况。因此上节具体“旧日fallback进入upsert”的阻断在**当前源码层已关闭**，不再作为保持维护的未修缺陷。父报告新增7例及联合测试正在跑，本子任务未代跑或提前声明这些测试成功。

在此前限定路径中，没有另外确认到仍改写历史StockKline/StockDaily原价格、既有新闻不可变版本、正式不可变run/snapshot或既有已成交原始凭证的具体入口。兼容PredictionRecord到期结果、当前FinanceNews/看板/行业映射投影及正常订单状态/持仓生命周期不应混称不可变原字段改写。

按父澄清，任务约束是修复过程不得手工下单，并非永久关闭用户原有paper运行；故正常消费者存在本身**不是新增阻断项**。由父继续完成联合测试、schema/触发器验收和原8000禁调度smoke的0交易改动验证，随后按原授权决定恢复既有调度。本结论仅解除已定位源码缺陷，不冒充实际部署或全程序形式化无副作用证明；本侧不执行生产操作，不继续扩展。

# Claw 双阶段 DSH 自动化实施（2026-10-01）

## 实施范围与生效状态

扩展既有本地 bundle 为1.1.0、MCP为1.3.1，保留四个项目 Skill，不新建平行策略/账本。
运行中 desktop profile 已加载22个MCP工具、3个契约资源及3个报告/安全工具。
[配置](<../.dsh/plugins/claw-research/cordis.patch.yml>)使用本机Python3.11和绝对workspace，
TZ=Asia/Shanghai、PYTHONDONTWRITEBYTECODE=1。报告插件绑定此项目，其它workspace/普通人工回合不受自动化门禁限制。

本机运行加载器对新增包入口存在已缓存解析失败；使用已验证的绝对file入口加载，
通过bundle重应用和该报告插件的显式禁用/启用验收active；同入口代码有ESM缓存，
仅重应用曾留下旧预算字段，受控回合发现后重新加载，并改用唯一物理runtime入口。
未重启DSH或Claw。一次重复 link 安装的包管理步骤退出0，
但管理器返回ambiguous-install；未据此认定启用，后续以实际fiber/工具目录及受控回合验证。

## 读取层

- [只读连接](<../backend/app/review/evidence_store.py>)：独立SQLite mode=ro、query_only、autoflush=False、
  显式BEGIN与语句护栏；不用应用写session，缺表/列不修复，错误仅返回缺证据。
- [模型读取](<../backend/app/review/model_evidence.py>)：替换5条有缺表DDL风险的模型HTTP链。
  保留冻结身份、指标、资格和部署artifact完整性/effective语义；不调用ensure/init，不回退HTTP。
- [分派](<../backend/app/review/evidence_dispatch.py>)、[执行/轨迹/市场读口](<../backend/app/review/evidence_readers.py>)：
  已存日历/就绪、12户原始交易与经济核算、逐周期、通知精确ID联结、逐股风险与候选日志、
  涨平跌/缺K/涨停炸板分页、近6日K。旧版本事实与当前协议实验分开。
- [材料读取](<../backend/app/review/research_artifacts.py>)复用原15:50/20:45出版文件；
  只验哈希/时钟与有界小节，不触发出版。目录32、原文件16MiB、MCP本地响应1MiB。
- [价格读取](<../backend/app/review/price_evidence.py>)：日K与内容观测目录、采样1m/5m。
  文件/轮次/source/received钟、预算、缺帧保留；不把累计量当分钟量，不补造完整分钟线。
- [全市场批处理](<../backend/app/review/market_batch.py>)：通过既有市场工具section=summary/features，
  对全部已存宇宙（最多20k代码）计算近6日K和采样分钟描述，分上涨/涨停与非上涨（含未知）对照。
  一次批调用按分钟文件流式读取，300文件/单4MiB/总128MiB，解压/行数另有限制；不逐股重扫整日归档。
  日K最近6根使用既有(code, trade_date)索引的逐代码有界seek，避免扫描全历史窗口排名；
  隔离测试验证与原window查询的完整材料/指纹等价。summary仅回传前12条文件引用，
  完整manifest SHA/文件计数/错误与省略数仍保留，不扩大响应。
  无缓存，各页重新观察，指纹变化禁止混页。summary机器统计不等于模型已对所有股票做因果归因。
- [盘前上下文](<../backend/app/review/overnight_evidence.py>)：expected/actual日期、前一冻结postmarket、
  跨休市PIT原文/分析版本、晚采/legacy元数据诊断、外盘最新投影与纽约参考钟。
  最新外盘投影usable_asof=false，US期货/完整盘后不可用，不认证完整隔夜覆盖。

仍保留9条已逐链审计的既有GET白名单（不是数据库写权限保护）；不新增账户/预案/新闻/日历冷缓存GET。
新增13条本地链（含上述5模型）使用独立只读连接或只读文件，不调用业务HTTP。
[工具契约](<../.dsh/skills/ashare-daily-review/references/tool-contract.md>)说明具体参数/限制。
GET只读标记不替代源码审计；未来共享接口变化需要重新审查。

## 盘前源与时区修复：代码完成，Claw部署另验

- [review新闻](<../backend/app/review/service.py#L871-L879>)改用既有PIT reader；
  盘前窗口上一实际可信收盘到截止，原字段保留，增加版本/覆盖。
  无版本旧FinanceNews不再当历史事实，旧测试fixture已改为真实版本/显式测试时钟，未恢复前视读取。
- [美股指数fallback](<../backend/app/dashboard2/external_sources.py#L120-L154>)按同symbol相邻有效收盘计算，
  不再拿open当昨收；乱序/冲突重复/缺前收/无效浮点保守处理。
- [外盘观察源](<../backend/app/news/sources/global_market.py>)改为固定AkShare版本实际导出index_global_spot_em。
  去除误当美国期货的domestic futures_main_sina；缺失/NaN不是0%。
  合成发布时间明确是上海采集观察，源quote/session未认证。
- [scheduler](<../backend/app/data/scheduler.py#L959-L1037>)仅8处研究/风格/GPT/新闻Cron声明及
  [既有研究出版](<../backend/app/data/scheduler.py#L1099-L1106>)显式Shanghai。
  不改交易触发器、采集频率、参数、风控、订单或持仓。
  该文件有既有并行修改，只合并这些局部timezone关键字，不重写整文件。
- 本机读取到配置 REVIEW_AUTOMATION_ENABLED=true、PREMARKET=08:45、POSTMARKET=20:35、
  REVIEW_GPT_ENABLED=false；这是当前配置解析，不宣称运行进程一定同配置。
  未重启Claw或触发producer，源采集连通/自然产物覆盖及真正live trigger时区待发布后验证。
- 不直接修tenbagger的GET预案或动态池；DSH任务使用新盘前上下文，避免旧午夜截止/冷缓存写链。
  既有页面预案并未因此完成同等隔夜融合。

## 自动化权限、报告和优化队列

[宿主插件](<../.dsh/plugins/claw-research/automation-runtime.mjs>)与[策略](<../.dsh/plugins/claw-research/automation-policy.mjs>)
识别CLAW_AUTOMATION_V1标记，只允许审定读口、Skill和报告输出。
shell、任意文件写、代码修改、网页/浏览器、子代理、日历同步、参数、训练、推送、
任务管理/插件变更均被宿主guard拦截；不是仅靠提示词。

自动回合最多80证据/读取调用或20分钟；MCP本地45秒、报告每个正文/manifest128KiB；
失败有限重试。可保存partial/failed停止原因，不提高字节上限或复制全库。
报告只在outputs/dsh_reviews原子追加；同phase/day/输入manifest/协议SHA幂等，输入变化另revision。
保持crash锁、as-of时区与日期校验；盘前09:15后不得complete。
complete必须ready+无missing+完整覆盖；本版隔夜覆盖未认证，盘前只能partial/skip。

优化工作项只允许pending_review，分类为evidence_repair、engineering_equivalence、strategy_hypothesis。
没有人工选定，不执行自动代码优化、部署、改策略/参数或扩影子白名单。
完整流程见[自动化协议](<../.dsh/skills/ashare-daily-review/references/automation-contract.md>)。

## 已启用的两个 DSH 定时任务

通过schedule_create创建，并由schedule_list精确回读，均为weekly/state=scheduled/deliveryMode=host，
绑定当前Claw工作区会话。只创建以下两项，没有增加Claw producer或盘中交易定时器。

| 任务 | 上海时间/星期 | 任务ID | 配置后首次调度时点 |
|---|---|---|---|
| 盘后复盘（只读研究） | 周一至五15:30 | schedule-dc7d5659-6fe2-4656-b9ac-71cf633fea10 | 2026-10-01 15:30（UTC07:30） |
| 盘前融合预案（只读研究） | 周一至五08:00 | schedule-a9739a9f-4a09-483f-aaf6-0352ee1dfa80 | 2026-10-02 08:00（UTC00:00） |

2026-10-01按用户要求将盘前由08:55提前到08:00，再将原盘后任务从21:45原地切换到15:30。
只保留以上两项，没有21:45/20:55重复任务；固定as_of、新闻窗口及保存截止与共享协议同步。
只读/休市/延迟护栏不变。Claw自身08:45盘前producer、15:45终态、15:50研究出版及20:35晚间快照未调整。
只读研究门禁现允许15:30开始；晚生成组件列为缺口/partial，不补采或提前触发producer。
08:00尚不可用的盘前快照同样留缺口。以下21:45/08:55受控验收记录保留原时点，不能冒充新时点自然执行验证。
本轮67项隔离测试通过、联网尝试0；仅重载只读MCP插件后，真实15:30历史截止查询由blocked_before_close变为partial。
两个任务和共享协议新增禁止整库副本/全量导出、每回合最多一个新报告及重试幂等要求。
这不是硬磁盘总配额：既有输出目录仍有大副本/持续记录，报告保存器及producer仍有增长缺口。
占用、边界和待审治理详见[存储审查](<dsh-review-storage-audit-20261001.md>)；本轮未删除已有数据或部署Claw业务源码。

timeZone均为Asia/Shanghai。星期规则只是触发器，交易日由只读已存日历再次确认：
10月1日等休市触发应保存skipped，不做交易分析；当前已存下一开市日为10月8日。
日历未知不自行联网同步，报告failed/partial。09:15以后盘前仅late_research或休市skipped。
两个prompt首行分别使用CLAW_AUTOMATION_V1阶段标记，要求宿主guard认证80调用/20分钟后才读取，
并引用现有Skill共享协议；优化只登记pending_review，不能修改源代码或交易参数。

这些记录证明定时任务已持久配置，**不证明未来自然任务已按时执行成功**。
应用/电脑需持续运行；还需观察下一次自然投递及交易日生成的报告/覆盖。

## 测试与真实读取

- 最终主回归12文件：**322 passed，1既有multipart弃用告警**（32.92秒）；
  [无联网审计结果](<../outputs/dsh_reviews/verification/final-indexed.zog9Qe/report.json>)显示
  network_attempt_count=0、passed=true。测试仅临时库，未写当前交易库。
- [Node门禁/报告测试](<../.dsh/plugins/claw-research/automation.test.mjs>)：**6 passed**，
  覆盖恢复/同回合不能解除、普通人工与其它workspace不受影响、80调用预算、幂等/追加及deadline。
- 四Skill quick_validate及实际skill加载成功；MCP self-test列22工具/3资源。
- 独立受控回合：guard_installed=true、active=true；仅请求bash no-op `:` 被宿主
  CLAW_AUTOMATION_READ_ONLY拒绝（未启动shell）。
- 实际2026-09-30、21:45 readiness=ready、快照91质量good、12户结算可见；
  日K非历史PIT/文件实际可用钟未认证，ready并不等于全证据complete。
- 最初全市场summary实际触发45秒超时；[失败报告](<../outputs/dsh_reviews/postmarket-2026-09-30-43040c22a201e353c10a03611f21db933d1c87421283cbbaa5fe28735351177b.json>)
  保留unavailable和未知分母，未重试或提高预算。独立只读定位显示原全历史window日K排名耗时31.16秒。
  改为索引LIMIT6 seek并缩小返回manifest引用后，[最新受控验收报告](<../outputs/dsh_reviews/postmarket-2026-09-30-b2d28dbb97cb0c63c98846ebc40340d166ea134c01386b3dac5f224919c12565.json>)
  的summary/features分别20.8/20.2秒正常返回partial，同一bulk输入指纹一致；
  10次工具调用，无重试/补采/预算提升，保存和精确回读通过。
  全存储5213股/机器描述已评价5213/代码预算未评价0，上涨或涨停2346、非上涨2867。
  日K可用5213、近日日K31269行；3股混/未知价基，2股不足6根。
  采样常规分钟1251120/1251120 code-minutes、缺失0；256文件/64,417,593字节/1,338,430行，
  文件和行错误为空。manifest回传12条、省略244条，完整哈希与分母保留。
  features只核2股，绝不称为全部逐股因果复盘；采样分钟存在不证明分钟内完整或交易所完整K线。
  这证明批处理实际可用，不证明历史PIT、完整账本/策略失败对照或自然定时任务成功；
  旧失败报告没有覆盖，最终报告complete_coverage=false、workitems=[]。
- 实际2026-10-01、08:55被已存日历识别为休市；下一已存开市日2026-10-08。
  最终护栏受控回合认证80调用/20分钟，bash `:` 被拒绝；
  [休市验收报告](<../outputs/dsh_reviews/premarket-2026-10-01-5033acaea6b70c6caaa7b7ed5c3786cbc63a452aa4e6463593595aa0766da082.json>)
  保存、精确回读及重复保存同ID通过；readiness指纹与最终材料指纹分别保留，非自然定时成功。
- 实际执行汇总read_only=true，default出现2个同名实例，返回account_missing_or_ambiguous；
  其余11户保留current stored valuation钟/freshness unknown，不选第一行或合并旧/新default。
- 新本地部署读取实际返回两legacy lane、integrity_ok=true/effective_active=false、
  automatic_promotion=false；没有晋级或DDL。

## 已知未完成的证据/业务问题

1. default同名账户需人工确认合法账户身份/旧账户隔离口径，不创建或合并账户。
2. 未接入验证过的US股指期货、全盘后session冻结与美国交易所休市/早收日历；
   ZoneInfo只解决参考时钟/DST，不证明市场开市。外盘最新缓存不升级为PIT。
3. 全市场同形态冻结失败分母及历史报价缺帧仍缺；采样K线、当前StockTag和日K修订不能补造当时因果。
   初版预算内分页/重点分钟研究不是全部上涨股完整分时穷举，未覆盖量在每份报告明确列出。
4. 候选影子sealed core仍不匹配，未更新SHA绕过保护：
   当前源码观察e4f03d49c178101454ac8f42fc4c6de7d6a0b44fea4c12500ccb45c767bbdf81，
   固定sealed值4a98174565cb0e80c70db4baf4dc69a48a033c1f10ae90213484860fcb892934。
   此核心存在其它任务并行改动，本任务不改；需冻结/等价审查后另立研究版本。
5. Claw producer源码修复待受控部署/自然任务验证；BrowserSkill连接、Context7、Excel均不在本轮主采证依赖。

无真实交易参数/风险/信号算法/订单/持仓/核算变化，无业务HTTP刷新、补采、结算、推送或Claw重启；
未改前端，未做页面验收。DSH任务只有应用/电脑保持运行才会准时投递，
离线回补与任务卡存在不等于当天实时研究成功。

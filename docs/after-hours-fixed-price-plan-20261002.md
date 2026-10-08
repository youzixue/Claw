# 盘后研究与固定价格纸面模式：分阶段交付

> **后续部署更新（2026-10-02 17:05）**：用户将目标限定为腾讯/新浪盘后强弱与次日研究，并批准非交易日部署。研究专用冻结包已在原服务加载、仅应用038研究迁移；交易代码保持9月30日上线基线，039/040与专用撮合链未上线。以下第1–21轮“未部署”等表述保留为当时源码交付记录，不代表当前研究服务状态。自然交易日仍待验收，旧严格撮合目标未恢复。见[部署结果](</Users/youzix/WorkBuddy/Claw/docs/after-hours-research-deployment-20261002.md>)。

状态：研究覆盖、专用意图生命周期、冻结候选时钟谓词、资源防重基础及专用回报只读识别已实现；第5轮接入仅服务内部的全笔原子成交链，第6轮补齐08:00多维统计与冻结输入校验，第7轮新增独立版本的部分分片研究规划，第8轮补部分候选冻结/时钟谓词，第9轮冻结当前paper分片费用预估与配置身份，第10轮增加供给账本候选的原单分片绑定校验，第11轮复用现有回报表新增独立分片结构协议与040数据库保护迁移，第12轮增加实际存储行的有界分片结构读取，第13轮复用原资源CAS核增加休眠分片回报验收，第14轮接实际分片只读消费者，第15轮接仅服务内部的分片broker/book/授权与原单累计CAS，第16轮接保留实际成交/资源消耗的专用撤余量与失效，第17轮新增明确离线SQLite快照的只读schema部署预检，第18轮增加已读盘前样本诊断，第19轮按正式条文收紧专用原申报价量子集，第20轮补科创股票/存托凭证原200股与100股片段的存储读取、第二片及撤余量边界，第21轮复用既有运行表记录三个研究作业的调度终态诊断；隔离回归结果见各轮记录及第6节。**生产盘后撮合仍未开放**：正式order-level来源为0，未接公开撮合API/调度，专用撤余量只核对已存实际partial回报，不产生撮合授权。本任务未执行运行库迁移、重启或受控部署，未做运行版本和真实交易日自然验收，也未开启任何账户盘后自动执行。

## 1. 时点：分析可以后移，证据采集不能全部后移

完整的基本面、技术面、资金面及消息面融合放在**下一真实交易日08:00**。保留当日16:05基本面截面和20:35冻结快照；下一日只引用当时冻结材料并加入截至盘前实际可用的新闻，不能用次日可变投影重建前日。

| 上海时间 | 功能 | 本次状态 |
|---|---|---|
| 15:01 | 保存15:00后、15:05前常规收盘价格和量基线 | 新增研究作业；缺失不补造 |
| 15:05–15:30 | 交易所固定价格撮合窗口 | 公开入口仍只登记人工意图；私有全笔/分片核均须有可靠专用来源，正式来源尚不满足 |
| 15:30 | DSH初步复盘 | 不变；不能借用15:35及夜间才取得的材料 |
| 15:35、20:10 | 官方独立盘后量额采集及复核 | 新增有界采证；不认证供应商不可修订终值 |
| 16:05、20:35 | 原基本面截面与冻结快照 | 保留原生产任务，不改交易授权 |
| 次交易日08:00 | 前日冻结维度＋盘后特征＋可用新闻 | 扩展只读盘前上下文；不是自动因子调权 |
| 08:31、15:31、20:31 | 专用未成交余量失效检查 | 原新增清理入口扩展为严格零/实际partial余量失效；不扫描买点、不下单 |

所有新增CronTrigger显式使用Asia/Shanghai。研究作业还检查已存完整交易日和官方休市约束，不同步日历、不猜调休周末。DSH原交易日滚动提醒未在本次改动中重设。

## 2. 规则与真实来源

沪深2026年规则于2026-04-24发布、**2026-07-06实施**。盘后固定价格范围扩至各所A股及ETF，15:05–15:30按当日收盘价、时间优先；**不是延长普通连续竞价**。盘后实际量额在结束后并入证券全天总量额。A股T+1及停牌约束不变。完整申报窗口沪市09:30起、深市09:15起，本v1不声称覆盖该完整申报窗口。

- [上交所正式规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)
- [深交所正式规则](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)

已在2026-10-02只读查询到2026-09-30的沪深主板、创业板和科创板四股样本：

| 样本 | 独立盘后股数 | 独立盘后金额（元） |
|---|---:|---:|
| 600000 | 44,500 | 421,860 |
| 000001 | 88,700 | 1,026,259 |
| 300750 | 15,500 | 4,512,205 |
| 688256 | 4,612 | 4,653,554.12 |

沪市使用`fp_volume`（股）、`fp_amount`（元）、`fp_phase`；深市使用`volumeAhT`（手×100）、`amountAhT`（元）、`tradingPhaseCode2`。深市`now`是本日最新价，`close`是昨收，不能混用。

- [上交所样本接口](https://yunhq.sse.com.cn:32042/v1/sh1/snap/600000?select=name,last,volume,amount,fp_volume,fp_amount,fp_phase)
- [深交所样本接口](https://www.szse.cn/api/market/ssjjhq/getTimeData?marketId=1&code=000001)
- [上交所字段展示代码](https://www.sse.com.cn/xhtml/home/2021public/querySearch/search_HQ_2021.js)
- [深交所字段展示模板](https://res.szse.cn/modules/marketdata/trend/template/trendRightDetailTmp.htm)

同花顺尾[9]/[10]在这四股上与官方及新浪独立字段对应，新增解析与显式日期方法；[8]仍unknown。映射是样本交叉验证，不是供应商全市场字段字典。正式调度首选官方独立字段，不采用新浪缺失前填或腾讯VWAP估计金额。

**不认证**：全A/ETF覆盖、接口SLA、9/30当时首次可用、15:30即时更新延迟或不可修订终值。当前采集仅覆盖支持的沪深A股代码，ETF/北交所不纳入本实现。

## 3. 研究口径与缺失

- 常规15:00基线、独立盘后量额、官方全天总量额分列。官方闭市日累计已包含盘后，**不能再加一次盘后量额**。
- 腾讯常规金额是既有VWAP估计，不升级为原生实测；没有原始字段presence证明时，腾讯常规量≤0记unknown。官方合法、完整字段中的盘后零可以保留。
- 新特征：`after_volume_ratio`、`after_amount_ratio`。分子或同口径分母未知则NULL；不反推主力净流入，不改生产因子公式/权重。
- `derived_regular_*`是“官方全天总量额−独立盘后量额”的派生值，明确不是15:00现场测量。
- 保存来源/版本、内容与响应哈希、source_quote_at、实际received_at、本地接受/available_at、质量状态和缺失原因。晚观测值不回拨到15:00或历史交易日的可用时点。
- `available_at`是本地接受钟，**不是物理commit回执**；`historical_pit=false`、`source_finality_verified=false`、`trading_authority=false`。
- 不可变、内容幂等追加；修订另存。SQLite阻止UPDATE/DELETE/OR REPLACE覆盖；重复相同内容保留首次时钟。最新坏hash/合同不回退旧good掩盖损坏。
- 盘前只用交易日历匹配的前日冻结维度，陈旧快照只作诊断。维度携带snapshot_id、日期、数据版本和哈希；新闻仍遵守发布时间与实际可用钟。

采集预算：默认最多6,000代码，每批2个并发GET，响应≤256KiB，单请求≤8秒且受剩余HTTP总预算限制，默认HTTP/限频预算1,200秒。预算耗尽仍记录剩余代码明确缺失。数据库持久化/缺失行写入耗时不包含在HTTP预算内。批次50条提交，不保存完整HTTP响应文件、不复制整库。读取最多20,000行、返回最多500代码（默认100），截断和覆盖分母显式，不称完整市场覆盖；本次未新增自动历史清理。

## 4. 专用纸面模式：当前能力与明确缺口

新增`order_type=after_hours_fixed`，默认仍`limit`，复用已有[交易API](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/trading.py>)与[统一service](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)。**公开入口仍只登记人工意图、等待、撤销和到期失效；第5轮私有全笔核未接公开撮合API或定时执行，不能称生产已可成交。**

已实现：
- 只允许显式人工paper意图及唯一已有活动的常规账户；不自动创建/重开账户，不进入Challenger、共享组合或第十三账户。
- 交易日、规则生效日期、15:05≤t<15:30登记边界、原风控、T+1、买限价≥参考收盘价/卖限价≤参考收盘价的保守条件；缺价格或对手队列继续等待或拒绝。
- 专用原委托价必须为精确0.01元档，数量≤100万股；本paper子集仅100股整数倍，科创板股票/存托凭证原委托至少200股。科创板法规允许逐股递增和不足200股余额一次性卖出，但本版本尚不支持这些例外；原委托最低量不限制合法100股成交片段或原生对手资源切片。
- 锁后重验账户数值ID与风险；完整单调请求→接受→验证→终验时钟；取消不能早于登记终验。
- 保留服务器接受钟，按锁内分配的接受序号作**本地FIFO诊断**；幂等重试不换位，跨请求时钟回拨不能插队；未知外部前排不当作零。
- 同锁＋CAS撤销零/已核验partial未成交余量；不回退既有经济事实或退还资源。登记等锁取消不留裸pending；一条坏合同或异常经济事实不会阻断其余合法意图失效。
- 普通deferred、涨停排队与15:45普通日终路径排除本模式；异常经济事实保留待核验，不强行覆盖。
- 普通成交合同拒绝非limit模式，原14:57门禁和`close`研究语义不变；十二策略未接盘后自动下单。

当前不具备：
- 官方汇总量额、未成交总申报量及普通五档都不能证明本单的对手、外部排位或可消费资源。因此**所有正常盘后意图`fillable=false`，不生成TradeFill/PaperTradeLog，不预留现金/持仓，不改变买卖数量**。既有风险链的账户估值/持仓天数刷新仍保留，不能称整个账户投影绝不写入。
- 腾讯15:00基线仅参考，`execution_authority=false`，不是可撮合官方未复权收盘价证书。
- 正式来源下的固定价实际撮合/部分成交仍未开放。第5轮内部核接全笔实际book，第15轮私有分片入口另以exact partial candidate绑定原风控、broker/book、费用及原子资源/原单累计CAS；没有公开或定时撮合入口。第16轮原cancel/专用失效入口可保留已核验actual partial全部成交并撤余量，不能用零成交分支掩盖非零或未核验经济事实；生产没有本任务产生的partial成交记录。

上线前仍须完成：可靠盘后order-level对手/完整外部队列（或可证明保守上界）来源验收、私有全笔/分片链与实际来源adapter的受控接入、运行库部署及真实交易日验收。分片/撤余量另版合同及现paper费用回归已在隔离环境实现，不代表生产来源或券商费用合同验收。独立分配、专用typed时钟、原子资源/账务与只读消费者已逐阶段实现，不能用隔离夹具替代来源验收。**不是放开一个时间开关或把本地FIFO当交易所FIFO即可上线。**

### 自动继续第1轮：纯order-level重放与资源分配提案

新增[专用分配核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_allocation.py>)，不复用五档模块，不创建新的交易service/账户。其能力边界：

- 服务端来源目录仍只有三项aggregate-only，order-level来源数为0；客户端的`queue_verified`、数量、source名称不能将其升级。
- 为未来经审计adapter定义完整15:05初始队列及无gap的新增/全撤余量/成交生命周期重放；验证固定价、实际外部时间优先、当日未复权收盘价和接收/可用钟、独立短时freshness、身份、单位和预算。
- 在完整来源夹具中，可生成收盘价的本地FIFO全笔**提案**；同侧真实前排仍活跃、队首资源不足、证据过时或损坏时等待/阻断。未成交撤销不算成交量。
- 资源按原生外部订单ID＋内容哈希＋股数offset切片；先扣外部已成交，再扣同一账户场景传入的历史提案，扣除采用区间并集，不重复减同一段股数。重复、重叠、超量、跨账户、跨日及重贴内容均拒绝，不跳过大队首填小后单。
- 提案冻结来源/可用钟、终点序号和初始队列＋成交/撤单事件的链式前缀哈希。后帧可以追加，不能改写已见历史或回退水位；同frame ID不能重贴不同内容。哈希只是完整性绑定，不是供应商认证。
- 本地意图终验钟必须不晚于当前cutoff，序号严格唯一；取消但有非零成交量的输入也拒绝。价格按精确分位quantize核验且浮点序列化不能改变原价，拒绝高精度亚分和不安全表示。
- 预算：来源与历史提案各≤2MiB、初始/生命周期持有订单≤2,000、事件≤10,000、本地意图与prior各≤500、本地证明文本合计≤2MiB、prior资源切片≤10,000；超限整体阻断，不能丢弃旧消费而继续分配。
- 输出永远`proposal_only`或waiting/evidence_blocked，`fillable=false`、`execution_authorized=false`、`fills=[]`。提案**不是持久化预留、真实源认证或成交回报**；没有数据库/券商/账本写入。
- 仅未来可靠来源和完整ledger/contracts接入后，才能把“已验收且与真实book回报原子提交”的分配升级为模拟成交。本轮未扩大普通授权。

[专用隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_allocation_20261002.py>)只在private目录monkeypatch下使用full-order-level夹具；正例不能冒充实际接口覆盖。新增reconcile来源能力诊断，默认仍零成交。只读审查发现并已补反例修复：同序号历史修订复活实际已成交资源、Decimal舍入亚分价格、未来本地验证记录；亦覆盖同原单prior重复、畸形对象、旧帧回退、预算边界/+1、trade-after-cancel与部分成交输入。审查者未运行测试，测试结果由主会话另行收集，不称修复后已获二次审查批准。

### 自动继续第2轮：休眠的回报绑定资源CAS基础

新增[资源消费核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)、[SQLite保护DDL](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resource_schema.py>)及[迁移039](</Users/youzix/WorkBuddy/Claw/backend/alembic/versions/039_after_hours_resource_receipts.py>)，局部扩展[交易模型](</Users/youzix/WorkBuddy/Claw/backend/app/models/trading.py>)和原授权scope的事务identity/一次性标记。**第2轮时资源writer未接service，普通ledger拒绝盘后模式。**第4轮仅接入历史识别；第5轮才加入同服务事务的专用typed guard与私有全笔链。正式来源仍全为aggregate-only，公开API/调度没有调用成交链。

- 单个“真实数值账户ID＋证券＋交易日”根CAS串行保护全部外部资源；source/version/session一旦绑定，当日不能换名称建立新池。不同账户仍是独立反事实场景，不能声称十二账户在真实市场共享或同时获得同一对手资源。
- 不按frame/proposal重置。每个不可变回报header唯一绑定真实TradeFill/PaperTradeLog，并保存整组原生外部订单ID、hash、offset/股数切片；一笔全量fill可以消费多个资源，切片不要求每段100股。
- 使用实际数据库订单、账户及book经济字段核验身份、价格/数量、原决定、手续费、回报ID与时钟；不以raw JSON、账户名或布尔证书替代真实关联。source生命周期前缀和全部历史分配重验；缺失/坏回报、超限或水位冲突必须阻断，不将消耗归零。
- 私有writer只flush，不commit/rollback、不创建账户/修改仓位/调broker。要求同task/db、精确冻结请求、同一个真实SQLAlchemy根事务及一次性专用ledger scope；CAS、回报和账务必须由未来service共同原子提交，任何失败整体回滚，不局部重试或将回报失败包装成broker拒单。
- SQLite不假定FK或recursive_triggers开启。保护根/回报不可删除、不可替换、不可重置，及已有绑定book经济身份；同时防INSERT OR REPLACE和从未绑定行发起的UPDATE OR REPLACE目标冲突，覆盖主键、fill_id/order_id及订单idempotency_key全部唯一键。非经济注释仍允许。writer逐条核对已装trigger实际DDL，而非只看名字。
- 历史读取≤500个header，单个proof文本≤2MiB，全部header及实际book必要列的UTF-8文本累计≤8MiB（含raw/risk、身份/状态字段和SQL原始日期时钟文本）。SQL先按字段类型/长度及剩余整行字节预算投影，避免SQLite不执行VARCHAR长度及类型亲和导致大字符串泄漏；不加载reason等无关字段。header逐条读取、立即计账，不预读未扣预算的后续整批。此为输入文本读取预算，不声称Python对象总内存峰值精确等于8MiB。
- 039消费迁移仅验证SQLite；非SQLite在建表前拒绝。有任意根或回报时拒绝有损downgrade。没有迁移运行库，也没有为PostgreSQL消费提供未经验证的保护。

[隔离资源测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_resources_20261002.py>)明确人工制造“未来专用ledger＋实际book行”夹具，仅验证消费、真实关联、CAS/回滚和DDL保护。它没有调用真实book/broker/risk链，不能把正例称为真实成交或已经完成整链集成。普通broker返回后原scope就会关闭，下一阶段必须局部延长原授权至真实回报完成（或严格的一次性同事务finalize能力），并同步补专用落账时钟、账本对账和买入层数typed消费者。部分成交/撤余量仍需另版合同。

首轮只读审查发现并已定向修补：既有book的UPDATE OR REPLACE目标替换，以及历史读取漏算实际book TEXT。第二次窄审静态确认全部唯一键已覆盖，又指出status等必要字符串未设SQL字节门、整批header预读未即时扣预算；已补全所有投影字段的SQL类型/长度/整行字节门和逐条header读取，并补巨大status、数字/日期列存TEXT、多header超限反例。两次审查者均未运行DB/pytest；最后预算补丁由本会话回归验证，不声称该补丁获得第三次审查批准。

### 自动继续第3轮：冻结候选与专用末端时钟谓词

在[现有专用合同模块](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_execution.py>)局部增加不可变候选输入与同步时钟验收；复用原分配核、原资源writer，不新增交易service或API。**第3轮时它们未接service/production guard；第5轮仅由原服务持有专用typed候选进入原子链。候选自身始终不是授权、资源预留或成交回报。**

- 冻结原意图、数值账户ID、实际决定钟及原决定轮次/策略版本、官方未复权固定价、来源生命周期与历史分配。原限价单独保留；实际候选价只能取来源收盘价。缺原provenance不猜测补造，部分成交/撤回状态不产生候选。
- 原数值账户ID与候选、资源合同必须为严格整数；请求数量拒绝与100数值相等的100.0，价格拒绝与1元数值相等的True。原无signal_id时按既有broker规则回退原order_id，资源核同步核验，不改变原订单信号字段。
- 候选使用有界JSON与长度前缀fingerprint；复验重放全部原输入，来源profile变更不能延长已冻期限。**fingerprint与重建只证明内部自洽，不证明发行者或供应商真实性**；未来service必须在自己拥有的原事务读取账户/订单/来源/历史并保持typed scope，不能接收客户端自贴候选。
- 锁后及紧邻变更前各自重新校验源TTL、日期、15:30终点和单调时钟，并在昂贵重放/哈希结束后再次采样；不能用15:29:59开始时钟跨到15:30落账。固定收盘价证据与短时对手/队列freshness不混为一谈。
- 产出的候选和时钟证据明确`execution_authorized=false`，`physical_commit_at=null`。普通`validate_ledger_clock`分支仍只接受limit及两种既有合同；第5轮新专用分支要求精确候选对象、冻结请求及原服务事务scope，单独JSON或客户端布尔不能打开它。
- 新增[纯合同测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_contract_20261002.py>)及资源核联接夹具：隔离测试先人工制造未来真实book行/私有scope，再验证候选时钟形状能被休眠writer与重启历史重建接受。**没有验证实际broker/book风控链，不声称完整模拟执行已打通。**

只读窄审指出同值不同类型请求（100.0股、True价格）与资源合同数值账户类型缺口，已补严格解析和反例。审查者未运行测试，补丁验证由主会话完成；不声称修复后获重新审查批准。初测另有两项测试包装错误（畸形JSON先被测试helper解析、同步测试进入async scope），已修正测试调用，未放宽生产授权。

当前公开人工登记已冻结服务端请求钟、`after-hours-intent-YYYY-MM-DD`专用决定轮次及当时策略版本，不继承普通腾讯轮次。第3轮此前对此处状态描述有误，第4轮按当前源码纠正并补直接验证。候选仍拒绝缺失provenance的旧意图，不能给历史订单补造“当时已知”的版本/轮次，也不能把普通腾讯轮次当盘后源。

剩余整链接入顺序：原service事务内读取可靠来源与历史→全部原风控和锁后重验→服务自有typed候选/时钟scope覆盖实际book→真实TradeFill与资源CAS同事务完成→专用历史消费者的真实链路验收→部分成交/撤余量另版合同。只读消费者识别已在第4轮接入，但不能替代实际book生成与原子提交。必须隔离broker.place_order失败包装与receipt/CAS/flush失败，不能把后者写成broker拒单或释放消耗。当前正式order-level来源仍为0；不新增十二策略自动交易授权、不接实盘。

### 自动继续第4轮：专用回报的只读对账与买入归因

局部扩展既有[成交账本对账](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_execution_integrity.py>)和[买入归因](</Users/youzix/WorkBuddy/Claw/backend/app/paper/position_policy.py>)，复用[资源核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)读取整组历史根；没有新增账户、交易API、成交授权或自动策略。

- 单个数值账户/证券/交易日根的全部不可变回报、实际订单/成交/费用、资源切片与水位核对通过，才发布该根的任一绑定。删除回报、坏哈希、错账户/原意图、未来或倒退根时钟、重叠消费、混贴普通合同和DDL保护缺失都阻断；不修复旧账，不重建为零消耗。
- 原意图必须继续满足完整FIFO身份及请求→接受→验证→终验时钟；登记创建钟不得晚于终验，终验不得晚于dispatch，根checked_at不得早于其已绑定的实际成交钟。历史识别不以当前新报价TTL否认已经完成的旧回报，但仍必须在本次cutoff之前。
- 对账从账本和回报两端读取，账户/方向同时比较实际book、原订单及固定价合同中的有界身份字段；不能因改写可变账户链接而隐藏另一账户的原回报。每次调用仅局部缓存已验根，不使用长期缓存或单段JSON自行授信。
- 买入归因仍保留缺失/歧义回报为独立账本买入的旧额度语义；盘后分支固定`scale_in=false`，不信任可变deferred标记或另外追加的自动日志，不能据此释放买入额度。同步旧谓词不能仅凭新合同JSON认可盘后成交；普通limit的原规则不变。
- SQLite盘后候选扫描只取有类型/声明长度门限的必要元数据；盘后raw/risk暂投影为NULL，完整证明仅由有界历史核加载，不预读订单reason/error_message。历史核按每根累计≤8MiB输入文本（包含根、header与book必要列），单proof≤2MiB；SQL先检查UTF-8字节和类型。扫描的短元数据在此证明预算之外，普通旧路径及跨账户候选总行数没有被重定义成全局8MiB预算；不声称限制数据库JSON扫描内部内存或Python总峰值。

[消费者隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_consumers_20261002.py>)人工制造未来完整回报/资源根，验证重启后的只读识别、坏历史、普通与盘后混合、额度标记篡改和SQL先验读取门。正例仍不是实际broker/book风控链验收。只读审查指出可变标记释放额度、未验原意图、消费者预读过大文本及根钟早于成交四项；主会话逐项修补并补最小反例，审查者未执行测试，不能称最后补丁获再次审查批准。

### 自动继续第5轮：仅服务内部的全笔原子成交链

在[原service](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)新增私有全笔函数，复用[原授权与事务](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_authorization.py>)、实际PaperBrokerAdapter及原book买卖；不新增账户、外部接口或定时撮合。正式来源仍只有aggregate-only，生产分支继续等待，来源payload不能自称order-level。

- 原数值账户、完整已存交易日及规则资格、原意图、原决定轮次/版本和全部资源历史在原交易锁内重读。只处理既有常规账户的显式人工全笔委托；不处理策略单、部分成交、Challenger或共享账户。
- 同一官方固定价下，仅跳过完整验证且已证明限价不兼容的前单；可成交但资源不足、部分成交、坏证明或未知前项仍阻挡后单。前排读取有合计2MiB证明/必要文本预算，不能无界加载或丢掉前排。
- 原账户对账、全部真实风控及锁后重验保留。固定价取来源当日官方未复权close，不用原限价/普通五档生成成交价。
- 专用scope绑定精确不可变候选、当前task/DB及原SQLAlchemy根事务，覆盖实际broker→book→TradeFill→资源CAS→委托终态CAS。原limit两类合同、14:57门禁及close研究模式不放宽。
- 新账务账户查询只认可原数值ID的唯一已有active账户，不创建/重开同名账户。T+1、入场买费分摊、佣金/印花税和P&L仍由原book计算。
- 风控完成→dispatch→账本末验→资源末验→服务CAS/末验保持单调；末验重新检查原来源TTL和15:30终点，用已验时钟写updated_at，不宣称物理COMMIT时间。
- 实际book回报和资源消耗、现金/持仓/费用、订单终态只在同一原事务提交。CAS/receipt/flush失败原样回滚，不经普通broker失败包装写成拒单；COMMIT失联不释放资源或重下单，完成订单只读核对后幂等回放。
- 实际broker原始trade_time是空格分隔ISO，历史夹具为T分隔ISO；资源核采用严格本地datetime解析比较，保留原raw而非改写经济时钟或哈希。
- [原子链隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_atomic_20261002.py>)使用临时SQLite与仅测试目录的完整order-level来源，实际调用风控/broker/book，未人工创建新成交TradeFill。隔夜库存和入场买费仍是明示历史种子；不能称真实历史成交或来源接口验收。
- 私有函数没有公开API或scheduler调用；可靠生产order-level来源、真实adapter验收、部分成交/撤余量合同和部署仍未完成。十二策略盘后自动执行保持关闭，目标继续进行。

只读审查提出跨阶段回退和不兼容头单阻挡两项，主会话已定向修补并补反例；另补最终同步来源重放后再采样，阻止重放跨到15:30才结束仍借用开始钟。审查者没有执行测试，修复后的回归由主会话收集，不声称已获得再次审查批准。

### 自动继续第6轮：08:00多维统计及冻结输入校验

在[盘前reader](</Users/youzix/WorkBuddy/Claw/backend/app/review/overnight_evidence.py>)增加只读 `research_fusion.statistics`，不另起计算引擎、不刷新数据、不改原指标公式或策略权重。基本面、技术面、资金面复用前日已冻结数值，消息面复用当前截止前可见的既有新闻PIT版本，盘后占比仅汇总已读取返回样本。

- 三个冻结维度各自核对原输入日期与日历上一交易日。错日、日期未知、failed/invalid/unavailable或非有限数值保留缺失；布尔不能冒充数量，巨量整数不能越过有限数校验。每维展示status、缺失字段、原snapshot/hash/可用钟、质量及统计范围；观察池PE和top30资金样本不是全市场。
- 新闻计数/情绪来自同一有界PIT读取，不把晚收到或晚NLP结果放进08:00；保留计数覆盖、截断、未知零新闻等原边界，不称确定经济受益或完整隔夜覆盖。
- 盘后占比中位数只使用返回且有效的占比，保留有效/缺失计数、返回数与已存代码数。缺失不填0，截断样本不升级全市场，不产生主力流向或成交授权。
- 冻结payload在SQL投影层用UTF8字节门控制，超过2MiB只取计数与NULL，不先加载大blob再拒绝；日期/时钟及版本/质量小字段也受先验门限制。必要版本和质量字段非空白，as_of不得晚于created。最新可见坏记录保留其ID并判invalid，不能因分析日期不一致被WHERE预筛掉后静默回退旧记录。未来尚不可见记录仍按截止排除。
- 新统计不认证单项历史PIT，不把可变行情倒填前日。原 `prior_frozen_dimensions` 保留快照诊断，不能覆盖新统计的缺失/失败/日期校验；[自动研究参考](</Users/youzix/WorkBuddy/Claw/.dsh/skills/ashare-daily-review/references/automation-contract.md>)同步说明优先使用统计及其范围/缺失栏。
- 16:05基本面截面、20:35冻结快照等当日必要采证保留；分析汇总集中次交易日08:00。新增长假后的08:00隔离用例使用明确未来fixture时钟，不能称自然到时已验收。
- 本轮没有修改scheduler、交易service或风险权重，盘后自动执行继续关闭，也没有运行库迁移/采集、重启或供应商接入。总体目标仍未完成。

只读窄审指出最新分析日期筛选回退、失败状态升级和空版本/质量三项；主会话已局部修补并增加对应负例，另用测试代理读取SQL投影结果，确认超额payload实际返回NULL而非Python层事后过滤。审查者未运行测试，不能称修复后再次审查批准。

### 自动继续第7轮：独立版本的剩余量分片规划

在[同一纯分配核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_allocation.py>)复用原来源重放和资源区间引擎，增加独立 `PARTIAL_ALLOCATION_PROTOCOL`。原full协议不接受partial状态，新partial协议也不能混用full历史或升级原冻结成交合同。新函数只输出规划，所有 `fills=[]`、`fillable/execution_authorized/partial_fill_allowed/ledger_contract_supported=false`；正式三个来源仍aggregate-only。**不是部分成交已接入账务或已开放撤余量。**

- 每片保存原委托量/限价、原接受序号/时间、原意图hash、fragment_index、累计前/后量和未成交余量。供给全部历史原单和有序片段；所声明的filled_quantity必须与全部prior片段精确相等，缺历史、重复片段、重贴身份、同值错误类型、超量或跨协议一律阻断整批。
- 未来研究v1片段只支持100股整数倍的保守子集；对手资源切片可为70股等原生细量。可规划量向下取整但不消费尾部碎量，明确剩余量；队首未完成时后单不能越过，撤回或完整证明限价不兼容才可移开队首。这里的所供filled状态与prior仍非实际receipt。
- 历史片段按全局时间和原同方向FIFO重验：不能缩小head的旧规划后保留later片段，从而绕过未满队首。原接受序号不因后续部分规划而重排。
- 来源保留初始/逐事件prefix hash与时钟，并保留每个原生外部订单的稀疏filled/canceled变更序列，存量为O(orders+events)，不为每个prefix复制全book。旧资源须当时已存在且位于该prefix的未成交、未取消区间，不能把已源成交的offset重新当可用量；后续真实trade也不能反向抹掉更早合法的counterfactual片段。
- 每个历史片段都核对原接受/验证钟、资源存在钟、prefix末事件钟、来源可用钟和冻结到期钟；最新frame变新不能让旧片段越过原TTL或15:30边界。同一源事件prefix只能追加，不能改写。
- 被供给的canceled研究状态需独立typed本地取消观察，绑定原单、已分配/未分配量与可用取消钟；缺失/未来/错误类型或片段发生于取消后即阻断。不把此观察称交易所撤单回报或服务撤余量授权，也不把已规划旧资源返还。
- [新部分规划测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_allocation_20261002.py>)只使用人工供给状态和完整order-level fixture，不写DB、不产生真实TradeFill、现金、仓位、费用或T+1权限。当前资源receipt仍全笔唯一，部分分片的持久化消费/费用/买入层数/T+1/撤余量CAS合同还需后续实施。
- 本轮仅修改纯分配核、新测试和本说明，没有更改授权、service、broker、schema、策略权重、API或调度，更没有部署或启用盘后自动执行。

只读窄审提出历史prefix原生状态、跨order历史FIFO、旧片段有效期及intent数字类型四项；主会话已局部补强并增加原反例。审查者只读未运行测试，末次所读版本早于最终修补，不声称修复后再次审查批准。

### 自动继续第8轮：部分候选冻结及专用时钟谓词（不接账务）

在[原意图/冻结合同模块](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_execution.py>)复用既有原单校验、冻结和同步重采样核，增加严格不同类型的部分候选；不另建平行交易模块。原full候选类型/版本、全笔回报格式及原ledger授权门保留，新部分类型不能通过现有scope的精确类型判定。

- 冻结目标原单、全部本地原单上下文、有序历史prior和原feed，保存完整上下文/历史hash。同名目标的状态/余量/proof必须精确匹配；每项原numeric账户和requested/created/terminal钟保持一致。供给上下文仍不是实际DB/receipt认证。
- 固定价始终取专用来源的官方未复权close，原限价/原总量独立保存。部分合同只用fragment_quantity、片段序号、累计前/后量和余量，不把所规划量写成实际filled_quantity。
- request_id按原数值账户、原意图hash、片段序号及累计前量派生，并受原40字符范围限制；换frame/dispatch/quote retry不改片段身份，下一片身份不同。纯key不是数据库唯一/发行源/实际receipt证明。
- batch中的更早纯proposal不能冒充已落账历史：只有同方向首个可规划原单可以冻结，后单需先有完整且匹配的历史状态。被证明不兼容限价的头单仍可跳过；未知/损坏proof不能绕过。
- 分片时钟谓词重放全部冻结材料，绑定假设请求的片段身份/量/价、原决策与本次来源轮次，重采样检查原TTL、15:30和回拨；返回独立PARTIAL_CLOCK_VERSION诊断，绝非LEDGER_VERSION。fingerprint仅自洽性，不是发行源认证。
- 五段冻结材料各不超过2MiB，新部分capsule另有共享8MiB UTF8总门限，冻结和重验均执行；不把字符数当字节数，原四段full协议未新增该总门限。
- 新合同始终execution_authorized/partial_fill_allowed/ledger_contract_supported/fees_certified=false。正式三个provider仍aggregate-only；仅局部补partial历史FIFO读取校验，未改资源receipt、费用、T+1、部分买入层数、取消CAS、service/broker/授权、迁移、API或scheduler。
- 只读窄审发现前一轮partial历史FIFO曾用accepted_at>旧source放行更早序号头单；墙钟回拨且头取消晚于旧proposal时，会把坏历史认可。主会话增加未取消/晚取消负例和早取消正例，先在隔离测试复现2失败、1通过，再局部移除该历史放行条件。接受序号优先于回拨墙钟；早于旧proposal且有完整取消观察的头仍可移开。原full分配分支不变。
- [分片候选隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_contract_20261002.py>)仅人工研究原单/历史和完整来源fixture；被测新函数不读取或写入账务DB，不调用真实book/broker、不产生部分成交。测试框架的初始化只在受隔离门保护的临时SQLite。旧原子链回归另行验证，不能把其全笔通过称部分账务已接通。

### 自动继续第9轮：当前paper分片费用模型冻结（不扣款）

在[同一冻结模块](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_execution.py>)为partial候选增加 `fee_preview`。复用原[paper费用助手](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/paper.py#L563-L573>)，仅为两助手增加keyword-only冻结参数入口；不平行实现公式、不修改原默认调用的费率设置或full成交合同。

- 预估采用官方固定收盘价×该片股数，不用原限价。佣金、卖出印花税和模型现金变动取原book计算结果；买入税为0。金额沿用既有float notional和Python round两位口径，不暗改为其他舍入算法。
- 冻结当前 `PAPER_COMMISSION_RATE/PAPER_MIN_COMMISSION/PAPER_STAMP_TAX_RATE` 三项、模型版本及canonical hash；参数必须有限、非负且非bool/字符串，费率小于1。当前配置快照不认证历史PIT，也不是2026券商真实费用/税率。
- 原模型明确是**每个PaperTradeLog模拟成交最低佣金**，不是同一原委托全日累计一次最低费。例如测试假设最低5元时，两片各100股可以预估5+5元，而一笔200股可能仅5元。差异显式展示，不静默替换现有账务口径；真实券商按原单费用另须确认并另版批准。
- 每个partial候选的owned proposal副本保存费用模型与hash；原纯市场allocator仍不认证费用。同原单的后续片须核对全部旧片的同模型/hash，历史缺费用身份、被重写或配置变更则拒绝；不同原单不假装共有历史配置。
- 冻结过程前后核配置，既有费用助手显式读取冻结参数，不再在预估计算内部临时读取全局费率，避免配置临时变化又恢复（ABA）造成金额与模型不同。重验重新计算并比较整个候选，终端clock样本后再次比较三项配置。配置比较仍只是观测，**不是全局settings锁或实际原子扣款授权**；原book未传该参数，保持旧默认行为。
- 部分候选/冻结/clock版本升至v2，旧v1不可被重解释为费用已冻结；市场partial allocation版本不变。原full类型、授权scope、资源receipt和实际book收费行为未改变。
- `status=modeled_only`、`fees_certified/book_authority/cash_reserved/partial_ledger_supported=false`，无实际现金、税费、仓位或T+1变更。实际partial费用/receipt/消费CAS/撤余量仍未接通，生产order-level来源仍为0。
- [定向费用用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_fees_20261002.py>)仅人工完整来源、研究状态和monkeypatch当前设置。本轮修改冻结模块、新费用测试、本说明及共享paper两处费用助手的可选参数入口；paper文件其余大量并行改动不归属本轮。没有修改settings/service/授权/schema、十二账户策略或运行库，未改默认收费公式或原账务调用。

只读窄审未发现稳定配置、原生助手下的确定反例，但提出条件性ABA配置/有副作用hook造成预估与模型标签不同；主会话以原助手hook隔离复现买佣金和卖税两项失败，再增加原助手可选冻结参数，partial传入私有不可变参数快照。不是声明存在运行库配置竞争或提供全局设置锁。审查者末读早于该补丁，不声称修复后已重新审查批准。另增加卖出多片/半分float round边界及输入不变性回归，保持原费用口径而非新费用算法。

### 自动继续第10轮：原单分片的供给账本候选绑定（不写DB）

在[原资源绑定核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)复用原经济身份/费用/时钟校验，新增纯 `_partial_book_candidate_binding`。其输入是调用者供给的order/fill/trade/account视图、冻结候选及同原单全部历史fill/trade对；**不是已认证数据库回报、真实消费receipt或落账permit**。本轮没有接入writer、service、broker、schema或原历史消费者。

- 目标原单全部冻结材料需精确匹配供给的pre-fragment状态，不允许把已取消/已改变的订单视图重用；片段的稳定请求、数值账户、原单ID、证券/方向/日期/固定价、供给trade关联字段、策略/信号和decision/fill轮次逐项一致。
- 同原单历史数目必须等于片段序号减一，逐片绑定原proposal、原请求、原时钟与费用；其数量和严格类型累计必须等于当前cumulative_before。fill_id、fill数值主键和trade数值主键分别去重；每片dispatch须不早于上一片供给book时间。缺片、乱序、重复/复用回报、改费用或改变身份均拒绝，不能从未知历史假定已成交0。合法延迟book只要在下一片dispatch前仍可通过，未用“一律拒绝延迟”掩盖因果错误。
- 原book fees与模型预估精确一致，买入P&L为None、卖出P&L必须有有限数值；该有限值仅作一致性检查，**不认证利润计算、入场费用分摊、资金余额或T+1**。当前设置变更仍阻断对应候选，不能以该helper替代实际锁后风控。
- supplied时钟诊断按已有pure predicate重演、原TTL/会话与cutoff核对；它检验记录内部时钟，不声称发生了当前物理落账或拥有ledger scope。
- supplied JSON先验门：每段≤2MiB，capsule＋当前risk/raw＋历史raw合计≤8MiB，历史对数≤500；不先解析超额历史。这是解析输入预算，不是对调用前SQL读取或总内存的认证。
- 输出仅 `validated_partial_book_candidate`，database_reads_certified/execution_authorized/ledger_contract_supported/durable_resources_certified/cash_change_certified/realized_pnl_certified 均false。原full经济绑定默认不启partial，原scope、资源writer、回报识别和消费协议仍拒绝该候选。
- 接入检查发现原partial请求40字符加既有broker的`fill-`会超过[fill_id字段](</Users/youzix/WorkBuddy/Claw/backend/app/models/trading.py#L58>)的40字符上限。只缩partial请求为35、预期fill_id为40，保留原稳定身份要素和broker前缀，不扩DB字段、不改broker或full请求；部分候选/冻结/clock版本升至v3，旧v1/v2不重解释。费用模型和市场partial allocation协议不变。
- [新候选绑定测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_book_20261002.py>)仅制造未来账本视图，不插入业务DB、不调用book/broker或产生部分成交。原full资源与实际全笔链另作兼容回归，不能冒称本轮部分整链验收。现有部分候选长度用例同步适配有意的v3存储边界；本轮源码仅修改原冻结模块和资源绑定模块，不动原收费助手/设置、十二策略、实盘或调度。

只读窄审提出供给fill主键复用、历史冻结早于上一book、历史顶层身份与底层book冲突、行费用/账户同值异类型四项。主会话先增加原反例，再局部补独立fill数值主键去重、完整因果链、顶层原决策/策略/信号/本地身份绑定及严格类型；另补trade布尔价格和原数量浮点别名。仅部分诊断分支加严，full默认合同不扩大。审查者末读早于修补，未运行测试，不称修复后再次审查批准。

### 自动继续第11轮：分片回报结构协议与不可变保护

复用现有[分组回报表](</Users/youzix/WorkBuddy/Claw/backend/app/models/trading.py#L115-L128>)，不另建账本/资源池、不增加列。其order_id本就不是唯一键，可以为一笔原单保存多个真实fill/trade分组；allocation、fill数值主键、trade数值主键的唯一约束继续保留。

- 新结构版本 `after_hours_partial_receipt_resources_v1_20261002`，在不可变payload顶层存原单数量、片段序号、此前累计、本片后累计、剩余及稳定请求；JSON数量身份字段要求integer，不能由布尔或同值浮点别名充当。afp请求必须为35字符（afp-＋31小写hex），fill-前缀后必须精确40字符。
- [数据库保护](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resource_schema.py>)仍要求scope、唯一active数值账户及实际order/fill/trade关联。片数量需为正100股倍数；pre-fragment订单filled_quantity等于before；before取已有同原单receipt关联的实际fill.quantity合计，**不把正在登记的current fill算入**；另外拒绝当前片以外同原单存在无receipt fill，防止遗漏孤儿回报；序号等于已有片数加一，after不超过原单，remaining精确相减。新receipt必须匹配scope下一revision，scope核对时钟不早于该book，历史实际时间不倒退、同原单固定价保持一致并与payload.fixed_price相等，不将原委托limit_price硬当成交价。JSON必须是无重复顶层key的object，UTF8≤2MiB，request同时核35字符和35字节，拒NUL截断别名；不允许JSON代替实际行。
- 同原单full/partial协议不得混用，不能将全笔回报降格成片段或在已有部分历史后再记全笔；第二条full也拒绝，以保持原full reader的“一原单一全笔”不变式。同scope多个原单交错时，scope revision累计全部回报、片数和数量分别按原单计算。结构版本不是typed scope/数据源/费用/利润认证；原full writer/只读识别仍拒绝partial版本，不能过滤partial历史后只读取full、归零历史消费或发布部分verified结果。隔离用例在partial hash正确、实际root身份合法时确认旧读核整体拒绝且不修复写入；不把这一步冒称实际分片整链。
- 继续使用原append-only、所有唯一目的键REPLACE保护及真实已绑定book身份保护；不要求外键或recursive_triggers开启，不级联删除回报释放资源。
- 新[040迁移](</Users/youzix/WorkBuddy/Claw/backend/alembic/versions/040_after_hours_partial_receipt_guards.py>)用真实SQLite SAVEPOINT原子替换变化的book-binding trigger，不仅依赖SQLAlchemy/Alembic逻辑BEGIN；CREATE失败须恢复旧guard。不重写表或旧回报；核验旧039保护完整、旧版本仅full且原单无重复full回报。未知旧协议/缺保护须先审计，非SQLite拒绝。非空partial拒绝降级，纯full历史可恢复旧保护且数据保持不变。039显式选择原legacy DDL，避免修改历史迁移的输出；ORM新建隔离库安装最新保护。
- 缺040或trigger不匹配时，原核的完整DDL校验将fail closed；没有隐式更新运行库trigger或自动迁移。部署需要039→040顺序与加载版本复核，不能用源码更改宣称运行中任务已生效。
- [新结构用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_schema_20261002.py>)在隔离SQLite显式制造book行和CAS/回报，**未调用实际broker/book/risk，未改变现金/持仓，未证明逐笔来源或partial落账授权**。本轮未改service/授权/broker/ORM模型或十二账户策略。

### 自动继续第12轮：有界分片历史结构读取（不授予成交或消费权限）

在[同一资源核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)新增只读 `_partial_receipt_history_structure`，复用原账户/root、元数据上限和SQL叶投影，不创建平行账本/交易服务。本轮只读取当前已存分片结构，不接service/授权/消费者/自动调度。

- 先验核SQLite最新保护、唯一已有active数值账户和完整scope身份，再读取整个scope。scope内所有回报须为精确partial结构协议；full/未知/损坏行不被过滤，不返回“空历史等于0消费”，也不修复、改写或补回报。scope revision必须等于完整回报数，root时钟不得早于实际片段或晚于cutoff；结束时重读root并比较，观察到并发变化则拒绝整读。
- payload需要正确canonical hash、无重复JSON键和严格integer坐标；真实order/fill/trade、broker回报唯一关联、账户/证券/方向/日期/限价资格/固定价、费用叶的有限性与两行一致、实际成交时钟及35/40字符请求身份逐项核对。**hash仅自洽性，不认证来源或许可**。
- 按原单fragment_index核完整连续片数、实际fill数量累计、剩余与实际时间非倒退；整个同证券/交易日scope的固定价也须一致，不能只在各原单内部价格一致而允许两原单成交价不同。跨scope原单回报或任何无receipt同原单fill都拒绝。最后核当前订单投影恰等于累计，不能将filled_quantity归零重新开始；未满原单允许当前partial/canceled，经济历史仍保留，**该状态不认证撤单合同或取消时钟**。两原单交错按各自原单计算，不混淆root共享revision。
- 每段payload≤2MiB、scope≤500回报，root/header/所需实际行/最终root重读的文本总预算≤8MiB；每次SQL先按UTF8字节/声明长度/类型门限返回必要叶，选中的文本拒嵌入NUL，避免SQLite length(TEXT)停止计数绕过声明长度；逐次计费后再读下一行，不批量预读未计费TEXT。raw_json、risk_json及reason/error_message不读取、不解析；因此这些材料的完整性/执行身份明确未认证，不能拿此函数替代原typed账本识别。
- 结果只为 `consistent_partial_receipt_structure`：execution_authorized、ledger_contract_supported、durable_resource_slices_certified、raw_execution_contract_certified、quota_binding_certified、fees_certified、realized_pnl_certified、historical_asof_projection_certified均false。不发布原消费者的verified binding，不计算资金/仓位/P&L，不读取当前费率重算历史，也不认证source生命周期或资源切片防重。
- 这是**当前存储结构**核对：cutoff只约束所核回报/root时钟；订单当前状态不代表该cutoff的历史PIT投影。本轮未把纯候选的false-authority材料升级为实际部分成交合同，也未开放partial scope/writer或旧full消费者。

[新隔离用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_history_20261002.py>)复用第11轮人工book行/receipt种子，只读临时SQLite；不是实际部分broker/book/风险链验收。共享helper重用保持原full合同，partial root仍令旧full识别整体拒绝；实际partial写入授权、durable资源切片验收和撤余量仍待下一阶段。

### 自动继续第13轮：休眠分片资源回报原子核（不开放授权）

复用[原资源核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)的scope、真实行关联、同证券日root CAS及分组receipt；不是新交易服务。新增私有 `_persist_partial_fill_resources`，默认全笔写入/识别仍不接受partial；原授权的精确full类型门不改，**当前生产代码不能签发分片ledger scope**。

- 未来实际分片回报与pure候选使用不同fill/timing版本。canonical转换只证明精确冻结parts内部绑定，所有JSON授权/费用认证旗标仍false，不产生许可。必须原exact部分冻结对象、同task/DB/物理根事务、原冻结请求及一次性ledger阶段；普通/旧full/纯candidate JSON、不同对象、rollback后autobegin新根或第二次消费均拒绝。
- 新fragment从实际DB读取完整同账户/证券/日期原单集合，含filled/canceled，不只target或活动单。整个root所有真实partial receipts与book/raw/timing、严格坐标、hash和费用模型核对；full/未知/仅结构回报不被过滤为零历史。全局原单fill/receipt数量与实际累计必须一致，仅豁免本次已flush的current fill；任何peer孤儿fill/跨根回报/投影归零拒绝。全部原单/实际priors与原冻结context逐项对齐，再重放专用来源生命周期/FIFO分配。
- 历史费用用合同内冻结完整model构造既有不可变费用参数，复用原paper佣金/印花税函数，不读当前settings重算历史。当前新候选仍必须通过当前费用策略重建与最后写await后的参数复查，不追认历史收费为现费率；不是券商收费或P&L认证。
- 受限真实book/fill先存在，随后kernel只flush：同root revision CAS→append partial receipt；未来caller还须以原before/status做订单累计CAS，最终一处commit。任何CAS、flush、参数漂移、过期或回拨失败必须由caller整体rollback，不能释放消费/局部重试或把失败改记broker拒单。helper不调book/broker/风控，不写现金/仓位或更新订单累计。
- 原冻结feed在入口变为私有JSON副本，写await中的共享行情更新不能延长原TTL。最后await后重验原scope/request/事务、当前费用和原feed；昂贵重放后再次采样终钟，仍要求原expires_at内。physical_commit_at仍未知，不声称设置并发锁。
- 复用原SQL叶/UTF8字节门及逐次读预算，当前必要行、完整历史、全部peer和root读共享8MiB预算；每proof/payload≤2MiB、≤500原单/回报。不读取账户无关strategy文本或订单reason/error_message；source/version/session切换不能为同账户/证券/日期新建零消费root。

[新隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_resources_20261002.py>)仅人工设置future scope并在临时SQLite制造真实行及可回滚经济投影；三片100＋100＋100、完整filled peer和重启历史均不通过实际partial broker/book/risk生成。这证明休眠资源核/原子回报边界，不证明T+1/风险/资金算法或实际分片服务接通。旧消费者依旧只识别full，partial根不能被其过滤/升级为verified额度。未改schema/迁移、service、账本授权、策略开关或运行库；生产order-level来源仍0，公开撮合/部分撤余量未接入。

### 自动继续第14轮：实际分片回报只读识别与两个旧消费者接入

在[原资源核](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_resources.py>)复用同一root、typed经济关联和有界历史读取，新增显式actual partial reader与消费者兼容入口；不创建第二账本，不改分片授权、service、book、schema/迁移或自动开关。

- 旧 `_verified_receipt_bindings` 默认仍full-only、按原order ID返回；显式partial reader只接受精确actual fill/ledger/receipt版本。兼容入口根据**数据库整个root**的协议计数选择full或partial，空/混合/unknown根直接失败，不靠客户端JSON或异常后回退其他协议。两种完整根统一按实际fill ID返回，分片不会因同原order ID覆盖先前片段。
- [账本对账](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_execution_integrity.py>)与[买入归因](</Users/youzix/WorkBuddy/Claw/backend/app/paper/position_policy.py>)只改盘后分支的reader和fill-ID lookup；原普通immediate/pending校验、日内风险/资金/十二策略不变。每片须实际TradeFill/TradeLog/原Order与数值账户匹配；同原委托片段用原order ID归属，`scale_in=False`，可变deferred marker不能释放买入额度。卖出不能成为买入quota凭据。
- 整个actual partial根读取后才发布任何binding：原intent完整hash/严格数量与限价、片段索引及实际累计/时间、固定价、实际费用与冻结完整model、source/version/session、来源时钟和序列、同序列prefix一致、原资源hash与切片无重叠、root revision/watermark全部核对。另核同账户/证券/日期全部有经济事实的原单和actual fills计数，包含filled/canceled；缺peer、缺header、孤儿fill、投影归零、旧pure candidate或只有结构header均不能被过滤成“0消费”。
- 补原单数量100股倍数及买限价≥固定价、卖限价≤固定价。不把成交固定价等于原limit作为必要条件；合法更优固定价仍接受。对损坏但自洽的限价9买入成交10、原数量150/实际片100两个反例已先复现，再局部补拒绝。历史费用只使用冻结model与原公式，不读当前settings或新来源catalog；旧source TTL不被重新应用到次日回报识别。
- partial和兼容消费者入口的识别总UTF8预算包含必需guard名称/DDL、账户名、初次root、全部header/必要真实book与proof、最终root复读，共≤8MiB；proof/payload单字段≤2MiB、root≤500回报。DDL先SQL数值预检，只返回必需名称并用CASE限制每字段，单独还有canonical DDL规范化裕量硬上限；无关trigger不hydrate。DDL单字段上限与JSON payload上限独立，缩小JSON测试预算不会误拒正常DDL。原显式full helper和阶段12结构读核保留原有book总量合同，schema也已变为有界读取；不能把新共享总预算冒称所有既存helper已统一改造。
- 末次root只要变化即拒绝整读，最终root本身也扣同一预算；测试只在第二次State读取改变revision并匹配末次错误，避免首次读失败的覆盖假阳性。所有函数no_autoflush、无修复/更新/commit；cutoff约束实际回报与root时钟，**当前订单投影不是历史PIT账户估值**。
- 此识别核证明当前持久化材料/实际经济行的关联自洽，不认证来源发行、逐笔外部历史的真实性、当前broker/ledger落账权限或P&L算法。source/hash不能凭此取得未来成交授权。正式provider仍aggregate_only，原authorization的exact full gate仍拒绝partial；不能因消费者可识别人工未来回报而称部分broker/book/risk链已接通。

[新消费者隔离用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_consumers_20261002.py>)复用人工future scope与临时实际行，覆盖三片原单、两原单一个root、历史fee/catalog独立、取消余量后经济事实、损坏整体拒绝、JSON-only无根、买卖限价、整手原数量、SQL预算B/B-1/巨DDL拒绝、pending不autoflush及精确末次root复查。未部署、未迁移运行库、未接实时逐笔来源或开放自动执行。

## 5. 文件范围与并行保护

独立研究：[核心口径](</Users/youzix/WorkBuddy/Claw/backend/app/data/after_hours.py>)、[官方源](</Users/youzix/WorkBuddy/Claw/backend/app/data/sources/after_hours_source.py>)、[不可变DDL](</Users/youzix/WorkBuddy/Claw/backend/app/data/after_hours_schema.py>)、[迁移038](</Users/youzix/WorkBuddy/Claw/backend/alembic/versions/038_after_hours_research.py>)。专用模式：[意图合同](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_execution.py>)。

仅局部接入既有[stock模型](</Users/youzix/WorkBuddy/Claw/backend/app/models/stock.py>)、[THS源](</Users/youzix/WorkBuddy/Claw/backend/app/data/sources/ths_kline_source.py>)、[scheduler](</Users/youzix/WorkBuddy/Claw/backend/app/data/scheduler.py>)、[settings](</Users/youzix/WorkBuddy/Claw/backend/app/config/settings.py>)、[盘前证据reader](</Users/youzix/WorkBuddy/Claw/backend/app/review/overnight_evidence.py>)、[交易service](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)、[授权](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_authorization.py>)、[普通paper日终](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/paper.py>)、[成交对账](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_execution_integrity.py>)、[买入归因](</Users/youzix/WorkBuddy/Claw/backend/app/paper/position_policy.py>)及[研究说明](</Users/youzix/WorkBuddy/Claw/.dsh/skills/ashare-daily-review/references/automation-contract.md>)。这些共享文件存在大量他人并行改动；没有回滚/整文件重写，不能把完整工作树diff归为本任务。

## 6. 验证及部署状态

测试仅在临时SQLite/内存迁移库，用fixture HTTP、时钟及日历，不发送真实行情/券商/推送请求，不复制生产库。

已覆盖单位/日期/状态、未知与零、总量重复相加、晚可用与长假、内容幂等/OR REPLACE保护、最新坏hash、请求总期限、显式上海时区、冻结维度与陈旧日期、模式兼容、普通幂等身份、并发登记/取消、等锁取消、时钟回拨、T+1、风险、坏首条后正常失效与普通终态隔离。普通交易/风控/账务20文件回归已回读：**1,028通过**（1条既有依赖弃用warning）；新功能与直接关联边界组最终结果见下，不以mock通过宣称实际覆盖或成交收益。

验证命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_after_hours_research_20261002.py tests/test_paper_after_hours_20261002.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_dsh_overnight_evidence_20261001.py tests/test_paper_public_boundary_20260914.py -q
```

首次阶段该组**195通过**；20文件旧交易回归**1,028通过**。自动继续第1轮在纯分配核审查修复后，以下7文件组合已回读为**296通过**（1条既有依赖弃用warning），并通过`git diff --check`：

```sh
python3 -m pytest tests/test_paper_after_hours_allocation_20261002.py tests/test_after_hours_research_20261002.py tests/test_paper_after_hours_20261002.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_dsh_overnight_evidence_20261001.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py -q
```

第1轮另复跑既有12文件（交易API、immediate/pending时钟、锁后风险、风控边界、原子成交、失联、账本对账、账户容量、资源回报、pending进度与入场费用分摊），已回读**646通过**（1条既有依赖弃用warning）。各组有重叠，不把上述数字相加成独立用例总数。该轮`git diff --check`通过。迁移测试仅使用内存/临时SQLite；没有运行库操作。

第2轮最终在SQL唯一键与完整读取预算补丁后，以下**25文件组合1,219通过**（1条既有依赖弃用warning）；资源测试含人工未来scope/账务夹具，不能冒充真实book/risk/broker端到端验收。该结果覆盖前述新功能组及原交易兼容回归，不与历史195/296/646/1,028数字相加。已回读后台任务最终exit code 0。

```sh
python3 -m pytest \
  tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_allocation_20261002.py \
  tests/test_after_hours_research_20261002.py tests/test_paper_after_hours_20261002.py \
  tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_dsh_overnight_evidence_20261001.py \
  tests/test_trading_api.py tests/test_paper_public_boundary_20260914.py tests/test_paper_immediate_boundary_20260914.py \
  tests/test_paper_ledger_clock_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py \
  tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py \
  tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_cross_order_ledger_integrity_20260914.py \
  tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py \
  tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_quote_round_execution.py -q
```

资源实现初测曾因局部变量`text`遮蔽SQL构造函数而出现5项失败，已改用`payload_text`并将异常负例收紧为HTTPException及明确状态码；没有为通过测试放宽交易/账务边界。最终包含FK/recursive_triggers关闭时所有唯一目的键替换、根事务回滚后重用、单次消费、跨session/version重建、防丢坏回报归零、CAS整笔回滚、非空迁移降级拒绝、B-1/B精确UTF8边界及多header拒绝前读取预算。

早期失败已保留：fixture股性字段/成交现金种子、HTTP宿主代理与mock绑定、日历实例monkeypatch恢复留下bound-method遮蔽等；修正测试隔离，不放松生产成交/风控断言。

第3轮候选与资源两文件定向隔离组已回读**96通过**；随后在第2轮25文件命令中追加`tests/test_paper_after_hours_contract_20261002.py`，完整**26文件组合1,271通过**（1条既有依赖弃用warning，226.15秒；均已回读后台exit code 0）。资源正例仍为人工未来账务夹具，不能替代真实成交验收；历史数字不相加。该轮`git diff --check`通过。

第4轮新增[消费者隔离用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_consumers_20261002.py>)和原登记provenance回归。定向4文件组（消费者、专用意图、资源核、原跨订单对账）最新已回读**183通过**（1条既有依赖弃用warning，57.20秒，exit code 0）。初测中未定义资源预算常量已统一为既有MAX_EVENTS；测试触发器筛选的LIKE下划线通配误选已改为精确GLOB前缀；删除fill的预期恢复为旧missing账本计数语义；混合普通合同夹具改用既有service JSON序列化维持原哈希顺序。没有为通过测试放宽普通或盘后授权。第3轮26文件命令追加消费者文件后，完整**27文件组合1,293通过**（1条既有依赖弃用warning，257.62秒，exit code 0）；另复跑下列原买入额度/策略身份/账户事务3文件，**147通过**（同类warning 1条，20.76秒，exit code 0）。两项后台结果均已回读。183、1,293及此前各轮结果存在重叠，不相加宣称独立用例总数；新正例仅为人工未来回报夹具，不是实际broker/book风险链验收。

```sh
python3 -m pytest tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py -q
```

该轮最终`git diff --check`通过；仅源码与隔离测试交付，目标仍在进行中，完整成交链、可靠order-level来源及部分成交合同尚未完成。

第5轮首个实际book整链测试发现原始trade_time的空格/T格式比较差异，5项失败经严格datetime解析修补后，原子链＋候选＋资源组已回读**109通过**。新增隔夜卖出测试的两项初次失败来自测试种子把会自行commit的旧投影helper包在db.begin里；已修正隔离fixture事务，没有修改卖出费用、T+1或生产helper行为。跨阶段时钟与FIFO修补后，原子链＋候选＋资源＋消费者4文件组已回读**140通过**（同类依赖warning 1条，50.09秒，exit code 0）；之后另加最终同步重放跨界负例并纳入完整回归。实际成交来自隔离目录的full-order-level夹具与真实原book链，不是正式来源覆盖，也不是收益测试。结果与此前各轮重叠，不相加。第5轮另复跑原买入额度/策略身份/账户事务3文件，已回读**147通过**（1条同类warning，28.05秒，exit code 0）。

第5轮最终沿用上述25文件回归命令，追加[候选合同测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_contract_20261002.py>)、[消费者测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_consumers_20261002.py>)及[实际原子链测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_atomic_20261002.py>)，完整**28文件组合1,317通过**（1条同类依赖warning，358.64秒，exit code 0）；包含最终同步重放跨15:30反例。后台结果已回读，最终`git diff --check`通过。1,317、140及另组147存在重叠，不相加成独立用例总数。

第6轮补丁后，盘前统计、独立盘后研究、MCP只读接口、原每日复盘、盘中日期和研究调度时区六文件组已回读**183通过**（1条既有依赖弃用warning，25.34秒，exit code 0）；`git diff --check`及研究技能格式校验通过。SQL投影代理用例确认UTF8精确门限/+1时分别取回原payload/NULL；日期坏latest、三字段空白、failed/invalid维度、前日冻结数值/截止前新闻及长假08:00边界均有定向隔离覆盖。183与此前各轮有重叠，不相加成独立用例总数。

```sh
python3 -m pytest tests/test_dsh_overnight_evidence_20261001.py tests/test_after_hours_research_20261002.py tests/test_ashare_review_mcp.py tests/test_daily_review.py tests/test_daily_review_intraday_dates.py tests/test_dsh_research_schedule_timezone_20261001.py -q
```

新增长假用例初测**1失败、86通过**，原因是10/8的未来fixture未推进隔离时钟，原新闻读取正确将截止压到真实当前时间，因而不读取尚未发生的新闻。修正仅在该测试替换两个模块的now，保留原实际当前时间上限及datetime类型校验，未修改生产未来截止门禁；随后六文件回归通过。新统计仍不是供应商PIT/全市场/收益认证，SQLite测试不代表PostgreSQL集成验收；本轮只修改[盘前reader](</Users/youzix/WorkBuddy/Claw/backend/app/review/overnight_evidence.py>)、[对应隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_dsh_overnight_evidence_20261001.py>)、[研究参考](</Users/youzix/WorkBuddy/Claw/.dsh/skills/ashare-daily-review/references/automation-contract.md>)及本说明，未改scheduler或交易service。本轮已向用户询问是否有可靠逐笔委托/撤单/成交来源；在来源验收前不会把聚合量额改造成成交证明。

第7轮部分规划/原full分配/候选/资源/实际全笔原子链/消费者/意图登记/普通public边界/原ledger时钟九文件组已回读**424通过**（1条既有依赖弃用warning，62.81秒，exit code 0）；`git diff --check`通过。这里的部分正例仍只是人工研究状态；实际broker/book调用来自原full隔离用例，不能把424通过解释为部分账务或生产逐笔来源已验收。

```sh
python3 -m pytest tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py -q
```

该轮首测3文件**1失败、134通过**，是新测试把纯冻结函数的ValueError误期望为HTTPException，仅改为断言其原拒绝错误；之后旧prefix重贴原反例在6文件组仍失败（**1失败、235通过**），因首次文字补丁未匹配而未实际应用该资源校验。主会话回读定位并补上native prefix可用状态，同时补历史FIFO、原TTL、原数字类型。稀疏状态计数测试又有一次**1失败、150通过**，是fixture的一项opening＋两项add＋两侧trade更新应为5而误写6；仅修测试计数。九文件最终回归通过，没有以放宽生产或跳过负例换取通过。其后追加确定种子的200组容量/委托量守恒与完整历史重放性质测试，部分规划＋原full分配＋候选三文件组已回读**160通过**（1条既有依赖弃用warning，1.80秒，exit code 0），`git diff --check`通过。200组是在一个测试内循环执行，不称200个独立pytest用例；160与424有重叠，不相加。生产源码在九文件回归后未再修改，后续仅增加性质测试及说明。

第8轮初测三文件**1失败、167通过**，失败来自新用例把源原单150股内合法的offset50..150自洽历史误期望拒绝；纯proposal/重贴hash不是发行源认证，不能凭此把合法counterfactual改判为坏证据。用例改为offset100..200明确超出原native150股范围，生产校验未放松。之后三文件已回读**168通过**（1条既有依赖弃用warning，1.75秒，exit code 0）。新增部分候选＋第7轮九文件的十文件兼容组已回读**483通过**（同类warning 1条，60.56秒，exit code 0），实际book调用仍只在原full夹具，不是部分账务验收。随后补完整部分capsule总UTF8门限回归，三文件组已回读**169通过**（同类warning 1条，1.80秒，exit code 0），包含总UTF8 B/B-1边界及full不受新总门限影响的用例；之后十文件组已回读**484通过**（同类warning 1条，61.83秒，exit code 0）。这仍是历史FIFO审查反例修补前的结果，不当作最后补丁验收；各组重叠，不相加。

只读审查唯一P2来自复用的partial历史FIFO：新三状态用例首先**2失败、1通过**（59项未选，1.67秒，exit code 1），准确复现未取消/晚取消头被越过、早取消仍合法。移除源钟放行后，三文件已回读**172通过**（同类warning 1条，1.76秒，exit code 0），另补携带坏later历史时冻结仍在队首的head也须拒绝。最终十文件组合已回读**487通过**（同类依赖warning 1条，61.07秒，exit code 0），含仍在队首head携带坏later历史的对照。该组与172/169/484等重叠，不相加。原full分配、全笔授权及资源写合同未更改，最终`git diff --check`通过。审查者末读早于这处修补，只读未测试，不称修复后再审批准。

第8轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py -q
```

第9轮费用预估＋partial冻结＋原full候选三文件组已回读**138通过**（1条既有依赖弃用warning，1.74秒，exit code 0）。随后第8轮十文件组追加费用预估与原入场费用分摊测试，十二文件组合已回读**569通过**（同类warning 1条，62.82秒，exit code 0）。包含现有完整原子全笔链、资源/消费者/旧public/ledger、原费用分摊兼容，以及新配置漂移、冻结内变化、终钟变化、坏参数、同原单旧模型缺失/重写和旧partial v1拒绝。新费用正例仍无实际扣款/部分book；569与138/487等重叠，不相加。没有修改原费用公式或收费配置。该569结果早于ABA补丁，不作为最终补丁验收；首轮随后增加输入不变性用例，三文件组回读139通过（同类warning 1条，1.74秒，exit code 0）。ABA买佣金/卖税两负例先回读2失败（33项未选，1.60秒，exit code 1），精确显示预估6元/模型5元、预估税2元/模型1元；冻结参数补丁后同三文件回读141通过（同类warning 1条，1.76秒，exit code 0）。最终结果见后续记录，历史组不相加。

在冻结参数不可变快照和卖出多片/半分舍入用例补齐后，三文件组已回读**144通过**（1条同类warning，1.74秒，exit code 0）。最终上述十二文件组合追加原paper API测试，**十三文件组合734通过**（1条同类依赖warning，83.43秒，exit code 0），覆盖实际全笔broker/book/资源回报兼容、普通API、T+1及费用分摊。后台结果均已回读；最终`git diff --check`通过。734与144/569及此前各轮重叠，不相加，不解释为实际partial账务或生产来源验收。新partial预估没有扣款，原book默认调用仍使用原设置；本轮未部署。

第9轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py -q
```

第10轮初测三文件组**1失败、169通过**，失败是旧测试仍断言partial请求长度40；本轮有意修复35字符请求＋`fill-`恰好40字符，与现有存储字段一致，仅更新旧长度断言及v3版本拒绝测试。只读审查后定向反例依次复现fill主键复用/同值错类型、历史顶层身份不一致及历史因果顺序错误；第一次布尔价格反例的源订单限价10与改后的固定价1不兼容，先修测试为源限价1，随后准确复现布尔别名仍被接受。局部补丁后六文件组已回读**266通过**（1条既有依赖弃用warning，40.41秒，exit code 0），不是实际partial落账验收。新增合法延迟book正例和当前/历史零费用布尔别名负例后，新候选绑定文件已回读**74通过**（同类warning 1条，1.89秒，exit code 0）；该组与266及历史组重叠，不相加。

第10轮最终在第9轮十三文件命令中追加[供给账本候选用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_book_20261002.py>)，**十四文件组合808通过**（1条既有`python_multipart`依赖弃用warning，84.76秒，exit code 0）。后台输出已回读，最终`git diff --check`通过。覆盖旧paper API、T+1/费用分摊、普通public与ledger边界、原full实际原子链及新partial纯候选。808与266/74/734等存在重叠，不相加；实际broker/book仍只来自原full隔离夹具，新partial用例只供给行视图，不证明partial账务/资源落地或运行版本已生效。

第10轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py -q
```

第11轮初测两文件**52通过、50项fixture错误**，全部新结构用例的隔离账户种子漏了必需initial_capital；补齐明确5万元测试种子，未改模型/业务账户创建或收费规则。新结构文件随后已回读**50通过**（1.94秒，exit code 0）。在源结构补强前新增孤儿fill、前片固定价冲突、root终验早于book三负例，已回读**3失败、50未选**（1.48秒，exit code 1），准确复现结构缺口；局部补齐实际旧fill覆盖、历史固定价与root时钟后，两文件组已回读**115通过**（1条既有依赖弃用warning，19.31秒，exit code 0）。

随后新增两原单交错片段、缺/NULL叶、NUL request、无DBAPI BEGIN时CREATE故障恢复、有效partial hash但原full读取仍整体拒绝等用例，结构＋原资源＋实际full原子链＋消费者四文件组已回读**170通过**（同类warning 1条，40.68秒，exit code 0）。这些结果有重叠，不相加；新结构正例仍为人工未来book行，不是partial实际broker/book/风险链验收。只读审查是方案与顺序检查，未运行测试，末读早于补强，不能称修复后再次审查批准。

第11轮最终在第10轮十四文件组追加[分片结构用例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_schema_20261002.py>)，**十五文件组合881通过**（1条既有`python_multipart`依赖弃用warning，84.81秒，exit code 0）；后台最终输出已回读，`git diff --check`通过。包含73项新结构/迁移隔离用例，原full实际链、普通paper API、T+1/入场费用分摊、资源回报与消费者兼容；与170/115/808等组有重叠，不相加。新增迁移版本ID符合当前Alembic版本字段长度，旧039升级仍输出legacy DDL。881通过不解释为实际partial账务、生产order-level来源或运行中部署验收。

第11轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py -q
```

第12轮初测两文件组**1失败、109通过**（1条既有依赖弃用warning，30.78秒，exit code 1）：新测试在reader的no_autoflush作用域外自行执行ORM count查询，将自己的pending测试种子flush，导致断言自触发写入。仅将测试count置于no_autoflush并移除pending种子；reader内无写入，生产授权未放松。随后历史结构＋原full资源＋partial schema三文件组已回读**183通过**（同类warning 1条，30.82秒，exit code 0）。补首读root显式剩余上限、多header逐次计费/64-byte root不hydrate及账户缺失负例后，新历史文件已回读**62通过**（同类warning 1条，13.38秒，exit code 0）。183与62及旧组重叠，不相加；新正例仍是人工存储行结构，不是实际partial成交。

第12轮首个十六文件组合已回读**943通过**（1条同类依赖warning，97.07秒，exit code 0），此结果早于下面的NUL/跨原单固定价补丁，不能当作最后补丁验收。只读窄审指出SQLite NUL绕过声明字符长度，以及末次root测试mock在首次读即改revision导致覆盖假阳性；前者不代表总UTF8预算超限/授权漏洞，后者仅测试覆盖问题。主会话将mock改为只在第二次State读取变更并匹配末次root错误（对照已通过），另补两字段NUL和同证券日两原单固定价冲突三反例，定向组已回读**3失败、1通过、61未选**（1条同类warning，2.75秒，exit code 1），准确复现。局部SQL叶加NUL拒绝、scope固定价一致性补丁随后实施，保留合法64字符汉字正例；审查者未运行测试，末读早于修补，不称修复后审查批准。补丁后的新历史/补充反例/原资源/schema四文件组已回读**191通过**（1条同类依赖warning，33.42秒，exit code 0），与183/62/943等重叠，不相加。最终回归见下。

第12轮最终在第11轮十五文件组追加[分片历史读核测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_history_20261002.py>)与[定向结构反例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_history_extra_20261002.py>)，**十七文件组合947通过**（1条既有`python_multipart`依赖弃用warning，100.05秒，exit code 0）；最终后台输出已回读，`git diff --check`通过。包含末次root精确复查、NUL拒绝/合法Unicode、同证券日固定价一致、66项新只读结构用例及原full真实原子链、普通paper API/T+1/费用分摊兼容。947与943/191/183及以前组有重叠，不相加。实际broker/book仍只在原full隔离用例；新partial结构没有调用broker/book/风险、没有扣款/消费/开权或自然来源验收。本轮源码仅修改原资源核，另增两个测试文件及本说明；没有修改service、授权、schema/迁移、账户/策略、调度或运行库。

第12轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py -q
```

第13轮初测两文件**1失败、73通过**（1条既有依赖warning，24.95秒，exit code 1）：新孤儿fill种子的自增主键占了current fill显式id=1，未触达目标业务拒绝；仅将孤儿测试PK固定99，未放宽业务。补完整filled peer/实际history、scope epoch、费用类型/当前漂移和总预算后，四文件组已回读**186通过**，后来两文件组**87通过**，均exit code 0。只读窄审指出费用策略及共享feed在最后写await后的两个缺口；定向当前fee漂移＋终钟重采样首先回读**2失败、35未选**（exit code 1），再补冻结feed副本、终验scope/fee及昂贵工作后重采样、五项跨await反例。补丁后四文件组已回读**194通过**（42.80秒），资源/原full/供给book/schema四文件组**241通过**（44.68秒），新增CAS不可重试/同日根不能切换来源/B与B-1硬预算后两个新文件组**46通过**（22.98秒），均1条同类warning、exit code 0。各组重叠不相加；审查者末读早于补丁，不能称补丁后重新审查批准。最终整组结果见下。

第13轮最终在第12轮十七文件组追加两个休眠分片资源核用例，并纳入研究采集、THS、盘前冻结融合及调度时区回归，**23文件组合1,097通过**（1条既有`python_multipart`依赖弃用warning，167.96秒，exit code 0）。后台最终输出已回读，`git diff --check`及两个修改模块语法编译通过。包括46项新增休眠kernel/原子回滚/跨await/根事务与B/B-1预算用例；原full真实服务链、普通paper API、T+1/费用分摊及研究/PIT行为回归通过。新分片正例仅人工未来scope＋临时实际行，未调用部分broker/book/risk；不能称分片整链、真实来源或运行版本验收。1,097与241/194/186/87/46及以前组存在重叠，不相加。本轮只修改原资源核、原冻结/费用helper，新增两个测试文件及本说明；未改授权、service、schema/迁移、策略或运行配置。

第13轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py -q
```

第14轮首个两文件组已回读**3失败、42通过**（1条既有依赖warning，40.60秒，exit code 1）：损坏fixture对不可变receipt执行ORM更新/删除，被ORM事件守卫先拦住，未进入目标读核断言。仅改临时库损坏操作为Core SQL，不放宽真实保护。随后新增反例测试曾因本会话插入了字面\\n产生collection SyntaxError（exit code 2，无测试执行），仅修正测试换行；两个自洽经济反例再测已回读**2失败、26未选**（3.66秒，exit code 1），准确复现限价/整手缺口，随后partial经济分支局部补拒绝。补丁后消费者＋原full资源＋休眠partial资源四文件组**143通过**（83.25秒，exit code 0）。共享budget补入schema/account后四文件组**1失败、162通过**（66.78秒，exit code 1），发现缩小JSON测试上限误影响DDL字段上限；仅将DDL硬上限与JSON预算常量分离，未放宽任何实际JSON边界。追加两原单/卖出及限价方向后，消费者＋原full消费者＋结构读核三文件组**114通过**（51.52秒，exit code 0）。均有1条同类warning，各组重叠不相加。只读审查没有运行代码，最后读早于限价/shared-budget修补，不能称补丁后重新审查批准。最终整组回归结果见下。

第14轮最终在第13轮23文件组追加[分片消费者测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_consumers_20261002.py>)及既有[主账户买入额度](</Users/youzix/WorkBuddy/Claw/backend/tests/test_primary_buy_quota_20260923.py>)、[执行信号/版本](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_execution_signal_version.py>)、[持仓风控事务](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_position_risk_transactions.py>)、[跨单账本对账](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_cross_order_ledger_integrity_20260914.py>)回归，**28文件组合1,335通过**（1条既有 `python_multipart` 依赖弃用warning，191.56秒，exit code 0）。最终后台输出已回读，`git diff --check`及三个修改模块语法编译通过。包含31项新partial消费者/经济反例/完整读取预算用例和原full实际服务链、普通paper API/T+1、费用分摊、研究冻结/PIT、旧额度及对账兼容；各旧组重叠不相加。新partial正例仍仅人工future scope与临时实际行，未调用分片实际broker/book/risk；不能据此宣布分片整链上线。当前轮源码范围仅原资源核及两个旧消费者的盘后lookup，新增一个测试文件与本说明，未改service/授权、schema/迁移、策略/配置或运行库。完整目标继续active。

截至第14轮未完成工作：可靠生产order-level来源与官方固定价可用性验收、该来源adapter受控接入、已成交原单撤余量/专用失效及竞态、受控部署和真实交易日自然验收。其中撤余量/失效与竞态已在第16轮补隔离实现，实际来源与部署/自然验收仍待完成。第15轮私有分片整链测试仍只使用经测试目录替换的完整来源，不代表生产来源已经具备对手/前排证据。当前总目标保持active，不能因私有原子链通过标记完整能力或线上部署完成。

### 第15轮：私有分片真实服务链（尚未开放生产撮合）

复用原[统一服务](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)、[typed授权](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_authorization.py>)、[paper book](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/paper.py>)及现有broker/风控/资源核；默认全笔与普通limit合同不变，不增第二套账本或策略入口。

- 私有partial wrapper显式选择片段模式，读同账户/证券/交易日**全部原单**（含filled/canceled）及完整实际receipt历史；任何孤儿、跨协议、缺片或归零投影都不能释放资源。每次最多派一个同方向FIFO头片，原单300股可按100+100+100累计。
- exact partial frozen对象经独立私有scope入口变为实际typed clock；原full入口不能接受partial对象，纯JSON/候选布尔标志不授予落账权限。原task/db/root transaction、一次性消费及锁后/变更前时钟守卫保留。
- 全部片段保留原decision signal与原TradeOrder.order_id；稳定片段request采用`afp-`+31位hex，fill为`fill-`+request（40字符）。只有原typed ledger scope下，原book幂等查询才按这对请求/实际fill匹配，避免第二片把第一片同signal成交当回放。普通/full信号幂等查询不变。
- 新片实际费用调用原佣金/印花税helper并传冻结fee settings；当前policy漂移在变更前、资源终验及服务最后CAS后均拒绝。按现paper合同**每个TradeLog片段**收最低佣金，不假称券商原单最低佣金只收一次。历史fee仍只读冻结模型。
- scope覆盖实际broker→book（现金/持仓/TradeLog/买费分摊）→TradeFill→资源CAS/不可变receipt→原单累计CAS；同一外层commit，任一错误整笔回滚本片，不重贴旧成交为rejected，不释放旧消耗。partial风险阻断抛出并回滚，不覆盖已有partial终态。
- 继续按官方固定价、真实可消费资源和本地FIFO匹配，不接普通五档。单片风险使用fragment数量，原委托数量/限价不变；T+1、版本加仓、卖出买费分摊/P&L仍复用原book。
- 跨片增加已存原单CAS/资源终验时钟下界；下一片墙钟回退到上一片已验证完成前即拒绝。没有物理commit精确时钟证明。
- 私有调用语义是**尝试下一片**，不是公开客户端的“重试上一片”协议。commit回执失联需待核对；同frame已消费资源不能重填，filled原单回放只读。若未来开放公开片段API，须另外绑定期待的fragment/cumulative-before，不能把下一片尝试当成相同客户端重试。
- 尚未实现已成交原单撤余量/专用到期服务，本轮不改现零成交意图清理、不挂任何定时撮合、不改十二策略/配置/实盘。现零成交取消证据尚未转换为partial规划要求的专用取消见证；在ALL context里遇到这种旧取消项会保守阻断，不过滤它伪造完整FIFO，下一阶段补兼容与合法撤余量合同。生产三个provider仍aggregate-only。

新[实际分片整链测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_atomic_20261002.py>)正例真正调用service、risk、PaperBrokerAdapter与book，临时SQLite、禁网fixture日历、目录替换完整逐笔来源；不人工制造scope/TradeFill。首组10通过。扩展四文件组初测1失败、148通过（25.44秒、exit code 1）：stock_info测试hook误在锁后risk阶段跨时段，抛出来源EvidenceInvalid而非预期book HTTP异常；仅限定hook到typed ledger阶段，未放宽源/时钟。跨片回拨负例先复现1失败（19未选、2.40秒），补旧CAS/资源终验floor；首个补丁四文件组16失败、134通过（19.65秒），显示登记时ORM自动updated_at不是该片CAS验收钟，错误阻断尚无成交的第一片。仅对已有经济事实的peer施加该已存成交CAS/terminal下界，零成交仍由原intent单调钟校验，随后四文件组150通过（26.06秒）。所有组均1条既有依赖warning，各组重叠不相加。另本会话一次测试命令因插入失败没有选中用例（exit code 5，19 deselected），不当作复现或通过。

主会话额外复现typed timing转换在纯谓词终钟后继续哈希的变更前边界：定向1失败（20未选、2.07秒），book曾完成但后续资源核拒绝并整体rollback，**没有持久化越窗成交**。授权核补转换后的最终钟/原expiry/当前fee复验，最终时间写实际book timing；避免把“最终回滚”代替“变更前禁止”。最终组合另记。只读审查复读时跨片缺口已撤回，未运行测试；后续timing转换补丁由主会话发现/复现，不能称审查者已再次批准。

typed转换补丁后的实际full/partial＋纯冻结＋休眠kernel＋消费者六文件组合已回读**224通过**（1条既有`python_multipart`依赖弃用warning，66.57秒，exit code 0），最终41文件回归结果见下；各组重叠不相加。以上不代表实际市场撮合、ETF覆盖、供应商时效或自然验收。

第15轮最终**41文件组合1,958通过**（1条既有`python_multipart`依赖弃用warning，479.61秒，exit code 0），最终后台输出已回读。执行前`git diff --check`及本轮四个触及模块语法编译通过。新增21项实际partial服务/broker/book用例，包含300股三片买入/三片隔夜卖出、逐片最低佣金与税费/买费分摊/P&L、同原signal不误认旧片、FIFO头余量阻塞、T+1、风险阻断保留旧片、broker/资源/原单CAS失败原子回滚、COMMIT回执失联、跨阶段/跨片回退及typed转换后变更前边界；并扩大普通immediate/pending、原子/不确定提交/事务归属/容量、API及研究/PIT回归。1,958与224/150/10及以前各组有重叠，不相加。只读窄审未给全仓安全或运行验收保证；末读后的typed转换补丁已由主会话定向复现并回归验证。全部实际partial正例仍依赖隔离目录替换的完整来源，正式三provider均aggregate-only、order-level数量0。

本轮修改原service与授权、paper book的局部幂等/费用适配、原资源核注释，新增一个测试文件及本说明。没有修改source provider、策略参数、费用配置、schema/迁移、调度或前端；没有部署/重启/业务DB调用。上述三处共享核心模块原有并行脏改动保留，未整文件重写或回滚。完整目标仍active，下一轮优先实现严格撤余量、专用失效及与片段成交的竞态。

第15轮最终命令（工作目录`backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py tests/test_paper_cross_order_ledger_integrity_20260914.py tests/test_paper_immediate_boundary_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py tests/test_trading_api.py -q
```

**未执行部署与自然验收**：本任务未执行运行库038/039/040迁移、未重启Claw、未调用运行中任务/策略开关的管理接口，也未运行实际盘后采集入库。没有回读运行服务的已加载代码版本或验证是否发生自动热重载，不能只凭源码或pytest宣布线上已生效。研究038的PostgreSQL DDL仅静态提供，未做Pg集成验收；消费039及结构保护040明确只支持已验证的SQLite，非SQLite拒绝升级。需要另行批准受控迁移/重启和运行版本核对后，才能在后续真实交易日验收来源更新时效、覆盖与下一次08:00报告。不能称15:30定时任务已包含本次新增因子或可以在盘后成交。

### 第16轮：实际部分成交撤余量与专用失效（源码/隔离验收，未部署）

- [原service撤单](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)复用与撮合同一个 `_paper_order_transaction` 和锁；取锁后重新读取最新剩余量。只对submitted零成交或partial合法整手累计进行CAS。terminal重放只返回unchanged，不称已重新认证成交。普通limit/queue/broker取消逻辑不变。
- 部分成交必须通过整个账户/证券/交易日实际partial root读核，核对原单全部实际fills与累计投影、既存CAS/资源终验及原intent时钟；坏hash、缺回报/额外回报、mixed/结构-only合同等保留待核对。历史核对使用冻结旧费用，不读新成交费率、不过期的新quote TTL、不刷新来源。
- 撤单只写原单canceled、局部取消见证和本地验收时钟；原filled_quantity、avg_fill_price、成交回合/原signal、费用、现金、持仓、原TradeLog/TradeFill/消费receipt/root revision不变。只撤qty-filled的余量，没有退还已消费对手资源，也没有交易所撤单回报。COMMIT回执失联保留原异常/待核对语义，不伪造undo或拒单。
- 专用reconcile同时处理submitted/partial余量，15:30起包括次日迟到失效；无跨日滚单或补成交，逐单异常保留errors、limit分页保留truncated。普通15:45日终路径仍排除该模式。未到收市的waiting只是缺对手的诊断，不是经济/撮合授权。
- [partial allocator](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_allocation.py>)只读专用取消见证，session_end可以闭市后/次日观察，但来源与实际撮合仍严格15:05≤t<15:30。之前service的旧取消合同只允许严格零成交兼容，不重写原观察；不能用它认部分成交或释放历史。
- 增加同钟成交/撤单的因果边界：历史片段在canceled_at同钟时，只有取消见证引用精确稳定的、已经核验的fill ID才可认作先成交。缺引用的纯proposal仍拒绝；不能简单把所有>=改成>来允许伪片段越过取消。
- 新[实际取消测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_cancel_20261002.py>)在真实私有service/risk/broker/book产生临时实际partial后取消，包含保留旧经济、FIFO余量及资源不退还、收市/次日、异常首单、回拨、COMMIT失联与同锁竞争。新[兼容/异常补测](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_partial_cancel_extra_20261002.py>)区分纯proposal反例与实际原单取消，覆盖旧零成交见证、失效原因/时钟、同钟精确引用、异常诊断/批次隔离。

第16轮初始取消＋原意图两文件组**2失败、65通过**（29.87秒、1条既有依赖warning，exit code 1）。一项损坏fixture误用trigger名字，未进入读核；改用实际af_bound名称并在损坏后恢复DDL。另一项真实同钟取消阻断有效后单：旧prior与取消同钟被拒；定向组**1失败、5通过**（6.99秒），随后增加精确已核验fill引用的边界分支，未泛化放宽纯proposal。四文件组合取消＋意图＋partial allocator＋实际partial链**154通过**（42.29秒、1条同类warning，exit code 0），输出已回读。各组重叠不相加。只读审查没有运行代码；额外指出CAS后非法钟和pre-end坏诊断可能阻断批次，已局部补HTTP409/类型隔离并加定向测试，最终结果另记。

本阶段没有启用provider、改变策略/费率/自动开关、运行任务、调用业务库、迁移或重启。正式provider仍aggregate-only、可靠order-level来源0；本阶段通过不等于市场成交、部署/运行版本或自然验收。总目标保持active。

补旧零成交取消观察兼容及审查提出的异常隔离后，取消/兼容＋原full实际链＋原full消费者四文件组已回读**79通过**（38.50秒、1条同类warning，exit code 0）。再补零投影/缺fill但仍有消费receipt拒绝、冻结历史费率不受当前fee变化影响、取消CAS零row/失联回滚及实际fill引用严格类型与预算、CAS后None/aware钟，取消两文件＋partial allocator＋实际partial链四文件组已回读**131通过**（47.00秒、1条同类warning，exit code 0）。各组合重叠不相加。只读审查末读确认此前三项反例已关闭；最后额外count/reference严校由主会话补测试，不能称额外补丁获得审查者再批准。最终43文件组合已回读**2,002通过**（1条既有 `python_multipart` 依赖弃用warning，507.34秒，exit code 0）。运行前 `git diff --check` 与本轮三个触及模块语法编译通过。包含44项新增实际撤余量/纯兼容反例，连同普通limit、原full/partial实际链、风控/T+1/费用分摊、资源/consumer/不确定提交/容量/API及研究采集/08:00冻结融合/时区回归；2,002与131/79/154及此前各组合重叠，不相加。临时SQLite/禁网fixture/完整来源仅测试目录替换，不认证正式来源、线上加载版本或自然验收。本轮只局部改原service取消/reconcile、原allocator取消历史核验、原waiting诊断，新增两个测试文件和本说明；没有改book/授权/消费者/schema/迁移、策略/配置或调度定义。

第16轮最终命令（工作目录 `backend`）：

```sh
python3 -m pytest tests/test_paper_after_hours_partial_cancel_extra_20261002.py tests/test_paper_after_hours_partial_cancel_20261002.py tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py tests/test_paper_cross_order_ledger_integrity_20260914.py tests/test_paper_immediate_boundary_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py tests/test_trading_api.py -q
```

下一阶段仍需：完整可信盘后order-level来源及官方固定价首次可用性验收、可信adapter受控接入、部署前迁移/运行版本核对，以及真实交易日研究采集和随后08:00报告自然验收。无来源时仍waiting/零新成交；不为完成目标伪造对手或前排。当前未执行部署/重启/运行库迁移，未开启十二账户自动盘后或实盘，总目标继续active。

### 第17轮：明确离线快照的部署前schema预检（非部署证明）

新增[离线预检CLI](</Users/youzix/WorkBuddy/Claw/backend/scripts/check_after_hours_readiness.py>)与[隔离反例](</Users/youzix/WorkBuddy/Claw/backend/tests/test_after_hours_readiness_20261002.py>)。本任务只在临时fixture运行此检查，**没有检查或复制运行库**，没有迁移、重启、创建账号、调用策略/采集管理接口。

使用前必须由运维另行提供一致的、已有的离线SQLite快照；工具不提供备份/复制机制。不要将运行中的数据库或简单复制的WAL主文件传入。没有sidecar和前后stat一致不证明数据库已停机或文件由原子备份取得，调用者仍负责offline前提。下列仅为操作说明，非本任务已执行的生产命令：

```sh
# 工作目录 backend；路径由运维指定，绝不默认 DATABASE_URL
python3 scripts/check_after_hours_readiness.py --snapshot /path/to/approved-offline.sqlite
```

- 路径必须已有、常规文件，任何 `-wal/-shm/-journal` sidecar（即使为空）都阻断；使用 `mode=ro&immutable=1`、连接局部query_only/trusted_schema设置，只读且不创建sidecar。不存在的路径不会创建数据库；未传明确参数退出2。
- 只导入两份纯DDL模块，不导入settings/engine/model/service，不启动应用。数据库读取限必要schema、列/索引元数据和alembic head，**不读取账户值、业务payload或历史回报内容**。
- 精确head为 `040_after_hours_partial_receipts`，不是迁移文件名；旧/未知/多分支head、缺表/必需book列、三新增表类型/nullable/PK/unique/CHECK漂移、必需保护器缺失/变弱均阻断。不修复、不stamp、不自动升级。
- 对保护器规范化仅整理未引用SQL的大小写/空白及IF NOT EXISTS，保留字符串字面量大小写和空格；协议常量被改不能靠全局lower掩盖。任何额外partial unique不作为已有完整unique的替代，也不能偷偷限制同原单第二片段。
- 接受UTF8快照；UTF16快照在读取任意DDL前明确阻断，因为SQLite TEXT→BLOB按库编码计长。所有实际返回的数据库TEXT（编码枚举、head、DDL、列/索引/leaf）共享2MiB预算；DDL单字段64KiB、head80字节、列/索引名160字节、类型和unique collation64字节。SQL窗口先计算整批行数与UTF8成本、CASE阻止过量TEXT物化，再按实际返回值计账，不在后验拒绝前超量预读下一批。
- 列/索引/leaf分别最多128/64/128，SQL VM每1,000步合作式进度回调的累计采样计数门限200,000步，查询前后及进度回调检查3秒截止；这不是操作系统I/O硬超时或SQLite内部DDL解析/页扫描的内存SLA。字节预算是返回的元数据TEXT，不包括SQL语句、数值计数、页扫描或SQLite内部解析缓存。
- 输出JSON到stdout，成功状态为 `schema_verified_not_deployment_certified`，退出0只表示所列offline schema门禁匹配。报告始终 `execution_authorized=false`、运行加载版本unknown、自动策略运行状态未验证、order-level来源/历史回报/natural验收均false；不验证业务row值或外键运行配置。即使schema通过，仍须另行受控部署和真实交易日验收，不能据此打开自动交易。

本轮初测 **1失败、23通过**（1.28秒、exit1）：缺guard反例误用了不存在的名字，已改为实际monotonic guard。补共享SQL预算与列/CHECK反例后的组 **5失败、37通过**（2.49秒、exit1）：四项单元fixture与CTE同名导致circular reference，已换私有CTE名；一项损坏unique fixture保留孤立autoindex导致SQLite先报告损坏，已改为结构有效但错误的unique。随后 **42通过**（2.90秒、exit0）。这些组合互相重叠，不相加。

只读窄审另外指出三个真实schema/预算缺口：原必需book列漏account.status，UTF16无法用BLOB长认证UTF8预算，额外partial unique会限制多片。账户状态反例先因fixture错误未进入探针（定向1失败、1通过），修正为实际VARCHAR(10)后定向 **1失败**复现旧代码错误放行；随后补必需列、编码门禁和额外unique拦截，仍不认证运行值/自动策略。窄审末读另指出同列名unique会把额外NOCASE/RTRIM语义折叠掉：新[补测文件](</Users/youzix/WorkBuddy/Claw/backend/tests/test_after_hours_readiness_extra_20261002.py>)四项定向全部失败（0.72秒、exit1），复现旧代码错误认证；改为有界index_xinfo的key/name/coll读取，仅BINARY unique可归并，保留DESC冗余同义索引，并把coll元数据也计入同一预算。该最后补丁未由审查者再次复读；两文件组已回读 **56通过**（2.13秒、exit0），测试结果不是静态审查的执行结论。主会话再补显式PK身份检验：旧代码只用PK+unique集合，可将原PK与既有unique交换而不改变集合；定向临时重建有效空表复现 **2失败**（0.72秒、exit1），之后分别核对Scope主键scope_key、其余表主键id，不把unique存在当作主键正确。最后两文件组 **58通过**（2.11秒、exit0），最终45文件组合另记。额外加入实际038→039→040迁移函数在临时库生成的DDL对照，不仅给ORM库贴stamp。排序口径补测之前的离线预检单文件组已回读 **48通过**（1.96秒、exit0），SQL门禁单字段/整批共享预算、head、缺/变弱保护器、nullable/CHECK/unique/必需列、UTF16、sidecar、不建库、不导入应用、CLI、时钟/步数/row预算及前后文件不变均有隔离反例。各组重叠不相加；排序口径补测前44文件完整组合已回读 **2,050通过**（1条既有python_multipart warning，408.13秒、exit0），编译和diff检查通过；不能把此结果当作后来新增反例已通过。

#### 部署和自然验收分层

| 层次 | 本任务现状 | 后续验收证据 |
|---|---|---|
| 源码与临时隔离测试 | 研究/私有模拟链及offline probe已实现，最终组合另记 | fixture不是交易所来源或运行库 |
| 运维离线快照 | 尚未取得或检查真实快照；工具不提供复制功能 | 明确一致offline文件、head/guard/schema探针JSON |
| 运行升级与版本 | 未迁移/重启/回读已加载版本 | 受控038/039/040升级及实际进程版本；不以源码hash代替加载证明 |
| 来源能力 | 正式3项仍aggregate-only，order-level为0 | 完整初始外部队列＋连续逐笔生命周期，官方固定价当时可用性、来源时钟/版本/缺口验收 |
| 自动策略状态 | 未改十二账户策略或自动开启；未回读运行开关 | 部署后明确核对paper-only/default-limit和自动盘后仍关闭，不能用probe false字段当运行读回 |
| 自然研究验收 | 尚未完成 | 后续真实交易日15:01基线、15:35/20:10独立量额，保留16:05截面与20:35冻结；实际available_at不回拨 |
| 次交易日08:00 | 只读融合隔离测试通过，不是自然报告证明 | 存储日历匹配的前日snapshot_id/版本/哈希及新闻发布＋实际可用钟，缺失/修订/截断分母保留 |

没有完整专用来源时，正常意图仍waiting/零新成交；不因上述任一层通过而跳过其余门禁。当前未要求或接收供应商密钥；可另行提供供应商名称/文档以判断是否满足来源合同。本轮仅新增一个offline脚本、两个临时测试文件和本说明，没有修改交易/风控/账务/授权/调度/策略/来源目录或038/039/040迁移源码。保留所有既有并行改动；总目标继续active。

第17轮最终45文件组合输出已回读：**2,060通过**（396.17秒、exit0），1条既有 `python_multipart` 依赖弃用warning。包含58项新增离线预检/结构反例及第16轮43文件的研究、08:00冻结融合、普通/盘后风控、账务、全笔/分片/撤余量/消费者回归；2,060与此前2,050/58/56/48及所有定向组合重叠，不相加。运行前diff检查与CLI语法编译通过。最后collation和PK补丁由主会话定向复现/修复/运行验证，不能称获得审查者第二次批准；测试不证明生产源、运行加载版本、实际快照或真实交易日自然验收。任务目标仍active，不以源码pytest通过冒充已上线。

本轮最终命令（工作目录 `backend`）：

```sh
git diff --check && python3 -m py_compile scripts/check_after_hours_readiness.py && python3 -m pytest tests/test_after_hours_readiness_extra_20261002.py tests/test_after_hours_readiness_20261002.py tests/test_paper_after_hours_partial_cancel_extra_20261002.py tests/test_paper_after_hours_partial_cancel_20261002.py tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py tests/test_paper_cross_order_ledger_integrity_20260914.py tests/test_paper_immediate_boundary_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py tests/test_trading_api.py -q
```

### 第18轮：盘前已读样本清单，绝不代替自然验收

新增[纯样本清单](</Users/youzix/WorkBuddy/Claw/backend/app/review/after_hours_acceptance.py>)及[隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_after_hours_acceptance_20261002.py>)；[盘前只读上下文](</Users/youzix/WorkBuddy/Claw/backend/app/review/overnight_evidence.py>)新增 `stored_evidence_acceptance` 诊断字段。清单只使用上下文**已读回的值**，不另开数据库查询，不采网络、不运行调度、不刷新或重建任何前日快照，不签发授权。原 calendar/kline/previous_postmarket/news/external/after_hours/readiness 和统计事实继续保留；清单缺失/不合法时仅其自身为 unavailable/partial，不回填0或改写组件。

[盘后只读样本引用](</Users/youzix/WorkBuddy/Claw/backend/app/data/after_hours.py#L265-L272>)补充 stage、quality_status、recorded_at 和 integrity_verified：后者仅表示现有reader已通过本地payload/身份/质量/时钟/哈希校验，**不是交易所发行身份、终值或历史当时可用性证明**。最新坏证据仍保留占位，不回退旧good；假/缺失引用不能成为完整阶段对。

清单检查：明确Asia/Shanghai naive目标08:00、存储交易日历及前日日期匹配，前日20:35后冻结的snapshot_id/版本/哈希和三个冻结维度，不能由当前spot重算前日；返回代码样本的15:00–15:05前常规来源钟＋同日官方闭市独立量额引用、独立量额/日总量及缺失、行身份链接；新闻发布和本地内容/分析可用钟必须不晚于截止，未分析但已可用内容可保留。明确官方盘后零可保留，空样本或unknown不能变成零观测。这里只核验选中的有界样本，未返回新闻、市场分母、个股基本面输入PIT均不认证。

最多500个代码和500条选中新闻；8MiB限制是传入**已经返回context的UTF8序列化**，不是新增SQL取回限制、序列化过程硬内存SLA或底层reader扫描预算。诊断并不改变既有reader的预算或过滤合同；它不采样当前墙钟，公开MCP仍由原cutoff门禁拒future。量的和式是返回整数样本的一致性检查，常规旁路保留腾讯100股精度，若精度/修订导致差异只报partial，不反推或改写原日总量。所有诊断分支始终：
- `natural_acceptance_verified=false`、`scheduled_job_execution_certified=false`；
- `recheck_run_receipt_certified=false`、首次来源可用钟/历史PIT/个股输入可用性/全市场/全新闻覆盖认证false；
- 运行加载版本unknown，`automatic_weight_update=false`、`execution_authorized=false`。

即使 `recorded_sample_checks_passed=true`，状态只是 `ready_for_operator_review`。15:01、15:35、16:05、20:10、20:35及08:00送达都继续列为未证明的job槽位；**相同内容的20:10复核被append幂等保留首次钟，不能从这条存储内容伪造20:10运行回执**。没有运行receipt表，本轮不补造该证据。9/4→9/8临时fixture特意在存储日历声明9/7休市，并非实际9/7交易日历或自然采集记录；隔离测试的HTTP也是MockTransport，不能证明供应商当时可用。

首组隔离测试1失败、139通过（13.21秒、exit1），失败因新fixture试图修改已flush的append-only DailyReviewSnapshot；已改为新建20:35快照、保留旧20:30记录，生产append-only保护未放宽。并将集成样本置于已过去的9/4→9/8存储休市间隔，避免把10/8未来截止与实际now上限混淆。修复后140通过（12.84秒、exit0）。窄审指出同observations IDs跨代码重用、非空新闻count为0/负/非int会错误通过，五项定向全部失败（1.37秒、exit1）复现，后补全样本ID唯一及严格count；日总量至少容纳常规+盘后股数也收紧。随后组147通过（12.43秒、exit0）。以上组合重叠不相加。审查末次指出date-only会被fromisoformat补午夜、关联ID用等值会接受bool/float；五项定向全部失败（1.39秒、exit1）复现后，改为完整日期T时分秒/可选1–6位小数、链接侧严格positive int，随后组152通过（12.91秒、exit0）。主会话再补精确微秒正例，定向1失败（1.75秒、exit1）发现正则转义错误拒绝正常微秒；已改用字符类[.]匹配小数点，最终46文件组合另记。最后严格clock/ID与微秒正例补丁未由审查者再次复读，不虚称第二次批准。

本轮仅研究reader的加法诊断和临时测试，未修改交易/风控/账务/授权/调度/策略/来源能力目录或迁移，未访问生产业务DB、未运行实际采集、未迁移或重启。正式order-level来源仍0，自动盘后未开启，部署和自然验收未完成；总目标保持active。

第18轮最终46文件组合已回读：**2,126通过**（396.76秒、exit0），含本轮66项样本清单/只读融合新例及第17轮45文件兼容组，1条既有python_multipart弃用warning。diff检查和三研究模块语法编译通过。另3文件MCP/日复盘兼容组 **84通过**（11.10秒、exit0，同一依赖warning），未访问业务HTTP/运行库；不与2,126或此前152/147/140等重叠组相加。测试不证明自然job已运行、上线加载版本、供应商首次发布钟或来源逐笔能力。微秒正例及关联ID类型收紧均已进入最终组合，但未声称审查者对最终补丁运行验证。

最终组合命令（工作目录backend）：

```sh
git diff --check && python3 -m py_compile app/review/after_hours_acceptance.py app/review/overnight_evidence.py app/data/after_hours.py && python3 -m pytest tests/test_after_hours_acceptance_20261002.py tests/test_after_hours_readiness_extra_20261002.py tests/test_after_hours_readiness_20261002.py tests/test_paper_after_hours_partial_cancel_extra_20261002.py tests/test_paper_after_hours_partial_cancel_20261002.py tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py tests/test_paper_cross_order_ledger_integrity_20260914.py tests/test_paper_immediate_boundary_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py tests/test_trading_api.py -q
```

附加兼容命令：

```sh
python3 -m pytest tests/test_ashare_review_mcp.py tests/test_daily_review.py tests/test_daily_review_intraday_dates.py -q
```

### 第19轮：正式规则下的原申报价量门禁，不是放宽交易时段

重新只读核对两所2026正式通知及附件；未访问交易行情、业务库或实际采集接口：
- [上交所2026规则通知](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml)及[正式DOCX](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/10816492/files/704204728fe74fff89de4f16efda4791.docx)：附件65,302字节，SHA256 `fc922c433438b2636cb631eab25cca405209712acbb6aaded768c45456ff8888`。3.7.6适用竞价数量条款；6.1规定科创板股票/存托凭证适用第6章，6.7明确盘后收盘固定价原申报200–100万股，不是科创板连续竞价限价10万股上限。3.3.11规定A股价格档0.01元。
- [深交所2026规则通知](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)及[正式PDF](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf)：附件282,084字节，SHA256 `9b66f8b0db70f84a25ef1ccb4ee2351001724e408117552d75f6d8993483c586`。3.6.7规定盘后买入100股整数倍、不足100股余额一次性卖出、单笔最高100万股，包括创业板；不能误套持续竞价限价30万股/市价15万股上限。3.3.11规定A股0.01元档。

附件仅在内存有界GET/解包读取，没有另存副本、重建业务数据或改TLS设置。首个Python附件GET因系统默认CA路径证书校验失败（exit1），随后使用certifi CA建立独立SSL context校验成功（exit0），没有禁用TLS验证。通知生效日期仍为2026-07-06。正式完整申报窗口沪9:30–11:30/13:00–15:30，深9:15–11:30/13:00–15:30；撮合仍15:05–15:30，15:00仍停牌不参与。**本轮没有扩大人工登记的[15:05,15:30)保守子集，没有放宽普通14:57合同。**

新增[原申报校验](</Users/youzix/WorkBuddy/Claw/backend/app/trading/paper_after_hours_execution.py>)，由[service早门禁](</Users/youzix/WorkBuddy/Claw/backend/app/trading/service.py>)在浮点规范化、账户查询/风控或新委托持久化之前调用；锁内意图冻结再验，FIFO原单证明核亦统一重验。[公开请求模型](</Users/youzix/WorkBuddy/Claw/backend/app/api/v1/trading.py>)另仅对专用MODE在Pydantic常规强转之前复用strict数量/精确cent校验，防止100.0或字符串100被强转成int、高精度价格字符串被提前舍为正常float；其原始数值错误422，板块原数量边界仍由service返回400。普通limit/market的字段强转与金额校验保持不变。这里检查JSON解析器已经返回的数值/字符串，没有自定义JSON数字解析器，**不认证已被外部调用者或JSON浮点解析器丢掉的原始文本精度**。数量须严格正int、≤1,000,000；原价复用现有精确分位与安全浮点序列化验证，不能先float吞掉高精度亚分；bool/float数量或bool价格证明也拒绝。主板/创业板paper最低100股，688/689科创股票/存托凭证原单最低200股，均暂只支持100股整数倍。冻结 `declaration_parameters` 明示规则身份、原申报范围、paper数量步长及不支持例外，**不是个股准入、官方逐笔来源或成交授权证书**。

保守子集不等于法规全能力：科创板201股等逐股申报，以及主板/创业板不足100股、科创不足200股的真实余额一次性卖出，现仍不支持。不能仅凭side=sell/客户端余额元数据绕过最低量。旧量不合法的专用意图/证明在读取或撤销/失效时会fail-closed待核验，不自动修量、清零历史或释放资源；本任务未读到生产存在这些旧行，不以隔离反例声称生产发生了错误成交。普通limit/market不走此helper，既有交易时钟、账户和十二策略不改。

原单最低量**不是成交片段最低量**：合法200股科创原单可分100股片段；原生50股对手切片可聚合成100股片段，不施加本地原单最低量/100股步长。全笔与部分planner、冻结候选/时钟核、实际回报原单绑定、撤余量通过原单FIFO核共享门禁，原单身份不能由100股fragment/request冒充。正式provider目录未动，仍只有三个aggregate-only项，order-level数量0。

[新增隔离测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_rules_20261002.py>)使用临时DB/API与私有order-level fixture；不是真实供应商或自然交易日验收。首组6失败/5fixture错误（1.58秒、exit1）：修复测试自身transitive trading_client未导入及状态标签误写后，15项原申报定向全部失败（2.86秒、exit1），确认科创100股、超100万和亚分原限价缺早门禁。补丁后6文件组323通过（15.04秒、exit0，1条既有python_multipart弃用warning）；后续增加精确100万、科创200原单冻结100片段与对手50股切片正例，追加公开model反例前的新文件58通过（3.98秒、exit0，同一依赖warning）。当前62例最终进入下述47文件组合，不相加重叠组。

只读窄审末次复读原申报helper/service早门禁/FIFO及新增175行测试，未报告指定窄段仍存在的静态确定实质问题；没有运行测试或写入，不构成运行安全保证。建议的可选覆盖增强为科创原单200→实际100→历史/撤余100/第二片100；本轮已有纯planner和冻结候选100片段正例，现实际链兼容组主要使用沪深主板夹具，不能因此宣称科创板实际准入或自然成交已验收。

47文件首轮兼容组合1失败、2,183通过（431.86秒、exit1，1条既有依赖warning）：唯一失败是旧partial消费者坏原单150股反例仅匹配账务层 `limit_or_original_quantity` 文本；现在先在FIFO原单门禁拒绝为 `unsupported original fixed-price declaration quantity`，拒绝行为正确，实际经济事实未接受。按damage类型改为精确匹配对应更早拒绝原因，继续断言两个消费者整体blocked/invalid，没有放宽生产校验；该测试断言更新未由窄审者重读。随后该消费者＋新规则两文件组89通过（30.23秒、exit0）。主会话再补公开model强转前闭环，三项API定向全部失败（2.68秒、exit1），100.0、字符串100和亚分高精度价格字符串确实被原模型强转并登记submitted（仍零成交）；新增专用model-before校验、精确422断言以及ordinary强转不变正例，随后规则/专用意图/API/公开边界四文件组187通过（21.68秒、exit0，同一依赖warning）；新增文件当前62例，最终重跑另记。

追加公开model-before补丁经第二位只读窄审者末次复读API/model/service/helper及当前201行新测试，未报告可静态确定的兼容回归、导入循环或类型反例；仍未运行测试/写入，不视作全仓或部署认证。

第19轮最终47文件组合已回读：**2,188通过**（412.81秒、exit0），含本轮62项规则/API/片段边界新例，1条既有python_multipart弃用warning；diff检查及API/helper/service三模块语法编译通过。与首组2,183、局部323/187/89/58等重叠结果不相加。最终测试仅使用隔离fixture/临时DB，不能证明供应商真实逐笔、运行库schema、加载版本、自然job或个股交易准入。此前失败均已说明，不称从未失败。

最终47文件回归命令（工作目录backend）：

```sh
git diff --check && python3 -m py_compile app/api/v1/trading.py app/trading/paper_after_hours_execution.py app/trading/service.py && python3 -m pytest tests/test_paper_after_hours_rules_20261002.py tests/test_after_hours_acceptance_20261002.py tests/test_after_hours_readiness_extra_20261002.py tests/test_after_hours_readiness_20261002.py tests/test_paper_after_hours_partial_cancel_extra_20261002.py tests/test_paper_after_hours_partial_cancel_20261002.py tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_extra_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_partial_history_extra_20261002.py tests/test_paper_after_hours_partial_history_20261002.py tests/test_paper_after_hours_partial_schema_20261002.py tests/test_paper_after_hours_partial_book_20261002.py tests/test_paper_after_hours_partial_fees_20261002.py tests/test_paper_after_hours_partial_contract_20261002.py tests/test_paper_after_hours_partial_allocation_20261002.py tests/test_paper_after_hours_allocation_20261002.py tests/test_paper_after_hours_contract_20261002.py tests/test_paper_after_hours_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_consumers_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_paper_ledger_clock_20260914.py tests/test_paper_entry_fee_allocation_20260914.py tests/test_paper_api.py tests/test_after_hours_research_20261002.py tests/test_dsh_overnight_evidence_20261001.py tests/test_dsh_research_schedule_timezone_20261001.py tests/test_ths_kline_source.py tests/test_primary_buy_quota_20260923.py tests/test_paper_execution_signal_version.py tests/test_paper_position_risk_transactions.py tests/test_paper_cross_order_ledger_integrity_20260914.py tests/test_paper_immediate_boundary_20260914.py tests/test_paper_pending_clock_20260914.py tests/test_paper_locked_risk_20260914.py tests/test_risk_execution_boundary_20260914.py tests/test_paper_atomic_execution_20260914.py tests/test_paper_execution_uncertain_20260914.py tests/test_paper_rejection_cas_20260914.py tests/test_paper_transaction_owner_20260914.py tests/test_paper_account_round_capacity_20260914.py tests/test_paper_capacity_receipt_identity_20260914.py tests/test_paper_pending_round_progress_20260914.py tests/test_trading_api.py -q --tb=short
```

本轮没有部署、运行库迁移、重启、行情采集、盘后自动执行或实盘；离线schema检查与隔离测试不证明运行加载版本或自然验收。总目标继续active，仍需可靠来源验收、受控adapter/部署及真实交易日采证与次日08:00验收。

### 第20轮：科创原200股、实际版本片段100股的隔离生命周期

新增[科创股票/存托凭证生命周期测试](</Users/youzix/WorkBuddy/Claw/backend/tests/test_paper_after_hours_star_lifecycle_20261002.py>)，分别使用688256、689009，共14项生命周期/风险断言与2项只读断言探针。**仅增加测试和本记录，未修改生产源码或科创既有风控**。临时SQLite中复用原kernel fixture人工构造订单、实际版本TradeFill/PaperTradeLog、费用、现金/仓位与typed future scope；这些行的实际版本是存储合同身份，不是真实broker/book/risk生成、供应商逐笔或交易所成交证明。专用资源kernel与只读识别、真实service撤单/失效及终态重放在该人工存储上验证。

- 合法原委托200股，首片100后保持partial，原量和声明证明仍200；即使来源TTL早已过去、公共读取钟和显式cutoff都推进到次日，历史回报及两个只读消费者仍按原单识别100股，不要求新鲜报价或签发新成交。
- 完整原单、durable历史与同来源追加对手在私有fixture下形成第二片100，原单累计200/filled、两条distinct实际版本fill/trade、同一root revision2；不把100股片段误套科创200最低原申报量。测试的逐片5元佣金/人工仓位只校验夹具与存储合同，不能替代真实book财务公式或券商收费验收。
- 用户撤余100、15:30失效、次日失效均只改变订单终态/取消证明，保留已成交100、均价、所有现金/持仓/trade/fill/root/receipt列，不退还资源。取消证明关联精确已核验fill ID，不能称交易所撤单回执。
- filled重放及terminal unchanged、canceled拒绝重放同时检查订单全部列、整个小型经济快照、session new/dirty/deleted和SQL DML事件；额外探针故意flush订单注释再由session关闭rollback，证明“事后快照相同”不足、SQL只读断言确能检出该临时写入。真实取消允许写订单，因此只对终态重放段要求全表不变。
- place_order、cancel_order及锁后risk都由禁止await的Mock保护人工存储链；取消不能误路由普通broker。正常公开API另在临时行情/tag fixture中提交合法科创200原单，仍由既有observe_only买入规则risk_blocked、零fill且经济事实不变；**不放宽科创准入、不修改十二策略**。
- 把原200损坏成100时，历史reader拒绝、取消409，经济消耗不释放。该反例也使filled100等于坏原量100，因此不夸称单独隔离了取消最低量与全部其他状态异常。

首组新文件2失败、12通过（10.93秒、exit1）：父人工row fixture未保存source，生产手工委托门禁正确403；现先在同一临时事务初始化source=manual再写book/receipt，不放宽生产门禁。定向修复组10通过、4 deselected（10.13秒、exit0）；之后增加SQL只读断言及2项探针，最终12文件兼容组已回读：**417通过**（139.32秒、exit0，1条既有python_multipart弃用warning），含本轮16项。diff检查及新测试语法编译通过；未改生产代码，因此不机械重跑第19轮47文件全组，不将417与此前2,188/定向10等重叠组相加。

只读窄审在252行旧测试版指出订单行快照及broker.cancel_order禁止调用两处覆盖缺口，另提醒“传次日cutoff但公共钟未推进”的断言范围；已定向补齐，并加SQL DML探针防关闭session后的rollback掩盖写入。审查者未运行代码/测试/写入；最后补丁未由其再次复读，不宣称最终再审通过。API/helper/service源码SHA256仍与第19轮最终回归时一致。

最终12文件命令（工作目录backend）：

```sh
git diff --check && python3 -m py_compile tests/test_paper_after_hours_star_lifecycle_20261002.py && python3 -m pytest tests/test_paper_after_hours_star_lifecycle_20261002.py tests/test_paper_after_hours_rules_20261002.py tests/test_paper_after_hours_partial_cancel_20261002.py tests/test_paper_after_hours_partial_cancel_extra_20261002.py tests/test_paper_after_hours_partial_atomic_20261002.py tests/test_paper_after_hours_partial_consumers_20261002.py tests/test_paper_after_hours_partial_resources_20261002.py tests/test_paper_after_hours_atomic_20261002.py tests/test_paper_after_hours_20261002.py tests/test_paper_public_boundary_20260914.py tests/test_trading_api.py tests/test_primary_buy_quota_20260923.py -q --tb=short; result=$?; printf '\n[exit code: %s]\n' "$result"; exit "$result"
```

本轮没有访问业务库、运行市场采集、应用迁移或重启；正式来源目录仍三个aggregate-only、order-level为0。私有人工夹具不能将其升级，自动盘后/实盘仍关闭，受控部署、运行加载版本和真实交易日/次日08:00自然验收仍未完成，总目标保持active。

### 第21轮：研究作业调度终态诊断与本次交付收口

相同来源内容的20:10复核会保留首次可用钟，不能用内容行证明20:10作业执行过。本轮复用既有ReviewAutomationRun存储三个研究ID的APScheduler终态事件诊断，**不是新建运行表、不是自然验收证书**。第18轮“没有运行receipt表”的描述应理解为当时没有这三类研究作业的专用事件记录；本轮不回填历史记录。

- 仅接15:01基线、15:35采集和20:10复核的executed/error/missed/max_instances事件。摘要只保留有界计数、状态、时钟和异常类名；不存异常文本或任意retval。真实handler开始/完成钟与事件观察钟分开；表的started_at/completed_at是事件观察钟，不是物理commit或历史数据库可用钟。
- 同一job/计划slot以逻辑身份去重。相同重投为duplicate，摘要改变为conflict；两者均不改首次行及其时钟。20:10即使来源inserted=0也属于不同slot，可独立留记录且不改来源首次available_at。
- handler的commit ACK与事件记录commit ACK明确分开。空universe没有来源commit，committed=False，事件仍可持久化；不能因此认定采集成功。disabled/busy/blocked/failed、错时钟/计数或错slot均不能生成已采集摘要通过标记。
- 独立写入预算5秒、owned任务最多16个。超额丢弃诊断、超时/失联ACK/停止取消记unknown；停止发生在协程首次执行前也由stop显式清除pending。没有采集重跑、删除来源、自动重试或交易写入。
- 复用既有迁移013的运行表；这里不运行DDL、不新建schema，不改cron、HTTP采集预算、供应商能力或十二账户策略。缺表只能unknown，不能自动创建。该表仍可变，字段不是不可篡改/历史PIT认证；PostgreSQL插入分支仅静态实现，本轮仅临时SQLite验证。
- natural_acceptance_verified、source_first_availability_certified、historical_pit、execution_authorized始终False。未接盘前receipt reader，既有样本验收旗标不升级；手工构造APScheduler事件、调用handler和临时HTTP禁网夹具不等于自然调度执行。

只读窄审提出事件身份去重、首次执行前取消、空universe假ACK三项诊断缺口。已分别用逻辑run_key与冲突保留首次行、stop显式unknown、实际commit返回标志修复，并增加4项回归。审查者的末次报告基于较旧230行测试版，未再次复读最终补丁，不宣称最终再审通过。

本轮初组65项通过（7.98秒）；前两项修复后的五文件组197项通过（15.13秒）。最终包含空universe回归的五文件组已回读：**198项通过**（16.39秒、exit0），diff检查与生产模块语法编译通过；这些组重叠，不相加。测试全为tmp_path SQLite、禁网/模拟来源及人工事件；未访问业务库、运行真实采集或重启。

#### 当前可交付与剩余边界

代码层交付：独立盘后/全天量额、缺失与来源/PIT时钟、次交易日08:00冻结维度及消息融合、专用固定收盘价/FIFO纸面核、部分成交/原子资源防重、撤余量/失效，以及只读部署预检和研究运行诊断。

后续只有三项外部上线工作：
1. 选择并审计可靠逐笔/队列来源及其字段、生命周期、可用钟和覆盖合同；正式目录仍只有三个aggregate-only来源，order-level为0。
2. 在明确批准的运行环境受控应用迁移、加载代码并核对版本；不默认打开盘后自动执行，不接实盘。
3. 等真实交易日留下当日必要采集冻结及随后08:00证据，核对自然调度、缺失覆盖和PIT边界。不能用单元测试或人工事件替代。

用户已指出运行时间过长。本轮完成已开始的诊断修复、测试及交付说明后收口，不再追加功能或新一轮加固。长期目标现已标记blocked并解除自动继续：已连续至少第19至21轮确认缺少可靠order-level来源及其审计合同，该外部条件不能靠人工夹具或重复测试消除。这不是目标完成，也不是未经用户请求把目标标记为paused；待用户确定来源及部署安排后再继续。




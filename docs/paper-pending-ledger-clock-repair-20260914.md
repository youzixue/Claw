# 第15轮：延迟/排队撮合的原证据时钟合同

实际工作跨至2026-09-15凌晨，文件名沿用9/14复盘目标。**源码阶段，不是已部署、实际成交量/FIFO认证或收益验收。**

## 问题与范围

第14轮已修显式round_id重贴实际上下文，但普通deferred、涨停queue仍以循环初始now/quote committed_at填充BrokerRequest.filled_at。统一成交锁、账户查询和费用查询之后，没有再检验真实时间；即时合同的两个账本检查点因为待单未传合同而返回None。原子事务正确不等于锁后时效仍有效。

本轮仅修改生产的三个文件：
- backend/app/trading/paper_public_execution.py：在原模块新增pending_fill_timing_evidence，复用既有报价状态、待买时限规则和本地日历/QuoteRound模型。
- backend/app/trading/paper_authorization.py：原scope和原两同步检查点支持显式pending类型，不新增授权ContextVar；即时合同仍要求decision_round==fill_round。
- backend/app/trading/service.py：普通/排队两条预检后的合同冻结、原事务内传递及逐笔回报保存。没有更改paper.py私有账本、risk engine、策略公式/门槛、models/migrations、调度或前端。

## 新合同与交易影响

1. pending_paper_fill_timing_v1_20260914明确scope为accepted_pending_quote_timing_only。status=validated仅表示时钟/轮次接受，不表示共享盘口容量或交易所FIFO已认证。它接在原版本/原路线确认/五档或queue触发/原风险链之后；不绕过原限价、100股、T+1、费用与风险。
2. 实际fill ID必须非空，与当前owned上下文/spot相同且健康；原decision ID非空且不同。决策同日、严格早于本轮提交；source≤received≤committed≤本次观察。检查本地已确认full会话（休市/未知关闭），真实Tencent QuoteRound来源、日期、提交/as_of/配置与代码版本与上下文一致；不联网补日历，不拿最新StockSpot替旧frame。
3. DB查询完毕重新采_public_order_clock；必须单调、同日且仍在**原观察所属**连续竞价会话，不允许长quote TTL把午前评估挪到午后。冻结原quote expiry、session end，适用时原确认confirmed/ttl/expires、原排队cancel cutoff。没有用较新报价延长旧证据。
4. scope沿用既有JSON参数名以兼容，区分immediate与pending类型。真实落账空合同409。lock_acquired与before_mutation均读真实clock、检验同日/单调/原期限；最后同步检查与库存首变之间无await。quote及原确认TTL保持“恰等有效，超过失效”；会话结束11:30/14:57为半开；queue原逻辑仍恰等cancel截止有效、超过失效。
5. 无法通过新预检时只记录waiting及原合同失败理由，不造fill。锁后失败仍由既有原子事务rollback、普通待单CAS拒绝/queue异常传播处理；不把COMMIT ACK未知改成假拒单。原订单decision/as_of、旧partial、旧持仓入场与历史成交未重贴。
6. 成功PaperTradeLog/TradeFill采用before_mutation_checked_at；queue诊断filled_at也取实际回报。每笔TradeFill.raw_json独立保存完整冻结pending_execution_timing及SHA对应的ledger_execution_timing；下一partial覆盖订单当前诊断不会丢第一笔证据。physical_commit_at仍为None，**不声称知道磁盘COMMIT时刻**。
7. queue无accepted/无fills不得确认为成交。queue开板全量/封板累计量仍是原近似模型，未借此时钟修复宣称可见深度或FIFO真实充分。

可能使原先凭缺失/超期证据产生的模拟填单转为等待或拒绝，改变未来订单、持仓、收益路径；不代表策略赚钱能力改善，不改写旧结果。

## 测试与阶段记录

全部pytest是临时/内存SQLite，conftest在app导入前隔离DATABASE_URL；关闭归档及调度。新时钟测试复用事务fixture的风险/入场策略mock，**只验时钟和真实账本事务，不冒充完整风险/策略验证**。另保留test_trading_api真实10规则排队链与既有联合风险/Challenger组。

- 新test_paper_pending_clock_20260914覆盖普通买/卖、queue开板/封板真实锁争用、最后账户/费用await、超90秒、午休、收盘、跨日、回拨/None/时区错误；原quote/确认TTL/queue截止恰等与+1微秒及配置变大不能续期；缺日历/轮次/健康/版本/三钟/原决策；跨查询午前借午后；逐笔partial原证据不变。
- 新paper_pending_fixture.accepted_frame为显式本地证据输入，不提供生产validator或风险mock、不重写spot时钟。旧需要实际成交的fixture明确补同观察日历/QuoteRound和独立验收钟。原负例仍保留，不能通过禁用新合同让旧测试通过。
- 首次新36例全失败来自测试漏填QuoteRound.expected_count（NOT NULL），**不是漏洞复现证据**；修齐fixture后新36+原50=86 passed/20.64s。
- 机械/原子/旧孤儿/轮次/新时钟五文件253 passed/70.71s。此前旧输入试跑因缺证据在锁争用前失败而停止（SIGTERM），没有当作完成测试。
- 扩展新测试初次140 passed/10 failed：6例时间字符串空格/T断言不符、4例测试在创建订单后改quote配置被原版本门禁正确拦截。修为原API格式及创建订单前冻结测试配置，150 passed/46.74s。未放松生产门禁。
- 初次全联合2513 passed/61 failed/335.59s，无skip；含上述10个测试输入问题和51个旧fixture缺失新时钟证据。本次保留失败日志，逐文件适配与最终结果见下方收尾；不将初次结果称为全绿。
- test_trading_api独立8 passed/4.37s，正queue情景固定9/14并提供本地证据，真实风险链未mock。原数量、限价、队前量与止损参数断言保留。
- 父五文件合同/退出版本/原原因/确认回归 **258 passed/47.52s**；八文件真实账本/原子事务/CAS/事务所有者/unknown/旧孤儿/时钟/交易API **442 passed/129.59s**（含102个pending新例、原即时50例及新增空合同1例）。随后新增queue accepted=false/回报为空但已经book的4例，真实经济账本回滚 **4 passed/4.03s**。本轮新增pending总106例+即时空合同1例；各组重叠，不相加冒充唯一总数。
- 适配provenance fixture时曾把context记录只写code，导致原_stock_info缺name，36例失败；补回明确fixture name/price后上述258组通过。原成交断言未删；唯原quote_calls准确从一次预检变为对接受分支多一次风险查询后的检查，断言显式计两次，不掩盖调用。
- **本轮尚不能报告全套通过**：首次联合剩余的34个T买回/continuous/deferred-confirmation旧fixture案例，已委托独立代理仅调整三个测试文件，父尚未接收最终产物/独立复验；另一个只读审阅未回收结论。下轮先验收这些分支，再跑完整联合，不把初次61失败或子报告当完成。
- 本轮封存为阶段清单stage_manifest.json；只包含父本轮已读写/核验的文件与日志，不将运行中代理文件哈希冒充冻结最终源码。生产三个改动文件可通过AST并记录SHA，paper.py哈希仍与第14轮相同；日志逐字节比对源/tmp输出。无前端改动，无本轮build/UI验收。

## 运维和独立剩余风险

01:02:26原8000仍PID67367（9/14 20:06:43），schema030。只读事务/query_only=1：110 PaperTradeLog、2258委托、293回报；交易全列compact-JSON SHA 84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357，同一快照前后及前轮相同。本轮未重启/迁移/生产业务API调用/下单/历史写入；同PID不能证明懒加载模块的完整版本。031仍需统一受控发布，不能把源码测试当成线上已生效。

独立未完成：B→C→B多轮逆序/重复容量、原decision QuoteRound全集和源钟推进证明、锁后最新证券身份/完整风险再验、跨订单/账户五档共享容量、queue开板量及封板共享FIFO近似；物理COMMIT/跨进程一致性、NAV与全链不可变原因/共享因子可用时间、K线消费者完整掩码、同预算时间外容量/退出研究及真实新交易日前向采证。目标保持active，不据本轮成功宣称全部完成。

# 正式预测路线合同只读审核（2026-09-14 / goal round5）

## 结论与证据范围

本次仅阅读当前源码；未改quality/promotion/batchdiag/tests/scheduler，未访问生产GET、运行业务或查询生产库。run87的2000实际开始20:07:24、完成20:08:25、1486 snapshots、batch gate=true与unknown_candidate_route来自父提供的现场结果，本子任务未重复核验。并行实现可能使行号/合同改变，以下说明审核时版本。

**news_catalyst_start、oversold_reversal_start、pre_board_probe_start是明确合法生成路线，不是非法字符串。** 当前PREDICTION_ROUTE_REQUIRED_DATASETS只列B/C/D三条消费路线，却被batch-health当成全体生成路线合法集合，混淆“合法但缺质量声明”和“真未知路线”。修改诊断措辞/注册表不等于证明旧run缺失的gate曾通过。

## 完整路线命名层次

`api/v1/promotion.py:116–132`的FIRST_BOARD_ROUTE_LABELS有15名；不是每个名字都是当前生成函数必然产物。当前首板生成主体11606–11654和二板17253的明确可生成集合为下列13名，是否实际落库仍受recordable过滤，不能把源码可生成集合称某次run实际分母：

| candidate_route | 生成依据/身份 | 建议共享全局数据依赖 |
|---|---|---|
| platform_relaunch | platform_relaunch_event_ready | 基础组 |
| support_squeeze_start | stealth_setup + support_squeeze_ready | 基础组 |
| fresh_mainline_start | fresh_hot判别函数按现阈值返回 | 基础组 |
| fresh_relay_start | 同函数另一分支 | 基础组 |
| news_catalyst_start | news_catalyst事件 / strict_news_catalyst | 基础组 + 保留逐股新闻PIT门 |
| auction_surge_start | auction_surge事件 / strict_auction_surge | 基础组 + auction_data（维持现合同） |
| mainline_spread_start | mainline_spread事件 / strict_mainline_spread | 基础组（维持现合同） |
| pre_board_probe_start | pre_board_probe / strict_pre_board_probe | 基础组 + 保留逐股试盘/K确认 |
| oversold_reversal_start | oversold_reversal / strict_oversold_reversal | 基础组 + 保留逐股反包/K确认 |
| quiet_setup | stealth_setup未走support分支 | 基础组 |
| relay_fillup | mainline_relay有记忆/最终默认分支 | 基础组 |
| hot_primary | capital/breakthrough非fresh分支 | 基础组 |
| second_board_promotion | 二板生成固定路线 | 基础组（维持现合同） |

“基础组”建议保守复用现已实现的`stock_kline/fund_flow/limit_up_pool`三个真实watermark，**这是待父落实的新路线质量声明建议，不是旧路线已有全局声明的事实**；这些是共同量价/资金/涨停题材输入。不得为了让旧批次变绿临时删依赖/降完整度。非交易观察路线注册不意味着新增paper交易能力。

另两项`fresh_hot_start`、`mainline_relay`在标签及兼容判断中仍被识别，但本次导航未找到生成主体直接输出它们。应作为**历史兼容合法名**保留原值/来源，不直接当未知非法；如共享注册选择canonical别名，必须保留raw_route、contract_version。fresh_hot_start不能无证一对一映射到fresh_mainline_start，它原先由sector/bull/support条件区分mainline与relay；不要用今天数据重新决定旧route。mainline_relay与relay_fillup同标签不构成历史质量等价证明。

### strict输入别名（不是持久化route通配符）

`_candidate_route_from_strict_route:11455–11479`的精确转换：
- strict_news_catalyst → news_catalyst_start
- strict_auction_surge → auction_surge_start
- strict_mainline_spread → mainline_spread_start
- strict_pre_board_probe → pre_board_probe_start
- strict_oversold_reversal → oversold_reversal_start
- strict_hot_fresh → 既有_resolve_fresh_hot_start_route动态分mainline/relay
- 未识别输入返回None，后续生成函数自行按已有事件分类；**不能在持久化诊断里照抄此fallback把非法字符串自动归合法路线**。

`support_squeeze_watch/other`属于watch bucket，`inverse_early_attack/news_catalyst/trend_reclaim/auction_surge/mainline_spread/low_absorb_halfway`属于rank_lane，`news_catalyst_first_board`等属于strategy_lane，`news_catalyst/pre_board_probe/stealth_setup/mainline_relay`可属于event_types。这些命名空间不得直接混入candidate_route。NULL、空白、非字符串仍invalid；任意未注册非空字符串是unknown/invalid，不能因batch gate=true放行。

## 生成、质量、消费现在如何关联

1. core/prediction_data_quality.py:190–195目前只建立4类watermark：stock_kline、fund_flow、limit_up_pool、auction_data。盘中K严格取目标日前日期；auction调用实际snapshot_health。`_build_route_gates:610–653`只遍历三条声明，要求依赖watermark为ok且满足现完整度，不存在/missing/degraded即阻断。全局gate由blocking findings计数决定，与每路线required dataset独立，因此全局true与某路线blocked/缺失可同时存在。
2. scheduler `_promotion_quality_gate_blocks_all_routes:130–143`首先全局true就允许生成；全局false时只检查PROMOTION_EXECUTION_ROUTES即B/C/D三条是否有通过。这是“是否形成研究/正式批次”的总屏障，不是对所有候选逐路线发交易授权。
3. 正式promotion生成要求显式quality_gate与合法时钟（17591–17598），生成/排序完整池后把quality_gate交_record_promotion_predictions（17741–17792）。当前未见quality.route_gates逐候选改ranking/probability的路径。_is_recordable_prediction_candidate:2236–2253允许二板完整池与首板shape_seed留证，因此“snapshot存在”不是actionable或质量通过。
4. paper.py配置358/368/380目前仅消费second_board_promotion/mainline_spread_start/auction_surge_start；4864–4895读取**所选不可变run metadata**。有route_gates时缺对应项或非True立即阻断、不回退；整个route_gates缺失时有旧版batch gate兼容分支。之后仍严格route/date/target匹配、trade_gate_passed、watch_only、actionable/rank及独立概率校验。C有原有当日rank/recall确认例外，不能在这次注册修复中扩大。
5. batch_diagnostics需要合法全集，而paper需要消费子集；不能扩大全体合法registry就顺手把paper配置/总屏障/候选排名替换成全路线执行。
6. route_rank_research.py:188–198读持久化route gate及fund时钟作研究，不该查询当前quality重建旧交易授权。

## 独立PIT/新闻/K线门不能被新注册替代

- promotion:8243–8273复用load_direct_stock_catalyst_map；禁止用可覆盖FinanceNews补候选，sector_inferred旁路已暂停。news/catalyst.py:80–164校验内容recorded/received/available、实体verified、分析available/completed截止、版本/hash及精确唯一实体。新闻gate缺证不等于市场没新闻。
- 当前没有news内容/分析全市场有效分母watermark；不要凭空在required_datasets加入`news`/`finance_news`并用行数>0作通过。最小合同应把现逐股新闻门列为candidate_evidence_contract，沿用结果/原因；若父要纳入批次质量，需要定义真实已有读API证据及版本，缺证unknown而非发明覆盖率。
- pre_board/oversold的形态分、试盘日期、strict_confirmation_count、provisional_last_bar以及逐股不足历史处理（如16235–16236、16813）仍独立。不得把基础K watermark通过当逐股价格链/PIT/可成交证明。formal_outcome_bar_error用于真实到期结果校验，不能反作盘中输入已知证据。
- auction要求不应顺手扩到所有名称提及“次日竞价”的路线；原逐股确认门维持，不以描述文字增设或删除阈值。

## 最小共享合同建议

只建一个纯注册来源（或复用父正在实现者），至少分开：`known_candidate_routes`、`legacy_route_names`、`execution_routes`、`required_datasets_by_route`、`candidate_evidence_contracts`、`route_contract_version`。质量模块、生成合法性、只读diagnostics消费同一注册；不从API导入大模块造成循环，不复制第三份标签表。新增合同只解决定义/解释/失败关闭一致性，不改probability、rank、Champion和订单策略。

诊断建议拆两个维度：
- route_identity = known / known_legacy / invalid_missing / unknown_illegal；
- persisted_gate_evidence = passed / blocked / missing_contract / unsupported_contract_version。

合法但旧metadata没有gate时可报告completed_route_gate_unknown（或明确合同缺失状态），不能继续叫unknown_candidate_route；也不能改叫completed。真非法仍invalid。所有数量保留在分母中，不能过滤未知行后给完整成功。

## 历史run87不得洗白 / 前向版本必要

run87合法路线被错叫非法是解释缺陷；其缺gate仍是**当时未留该质量合同**。允许现在只读纠正“合法路线”识别，不得更新run87 metadata/snapshots、补写route gate或使用当前水位回算其20:07质量。新增registry不意味着旧run已有新合同。

新质量报告/新run metadata应冻结route_contract_version（必要时contract hash及逐路线required datasets），诊断展示recorded与current解释版本。仅改字典不改变模型概率时无需伪造模型新训练版本，但至少质量/route合同版本和实际source版本要区分。旧run无版本应明确legacy_unversioned，禁止拿新contract默认值覆盖。发生技术修复后仅原允许窗口内新增真实attempt，不复用旧run_key替换；已过窗口不catchup。消费旧gate_map完全缺失的legacy batch-only分支是现行为，是否收紧由父独立决定并测试，不能称它已具逐路线证据。

## 需要回归的最小清单（本任务未执行）

1. 当前13可生成值及2兼容值全部有身份定义；与15标签集合对照；3消费路线仍为原子集。strict转换不把watch/lane/event误收，fresh_hot动态别名不前视归类。
2. run87形状：合法路线+batch true+缺gate → 合同缺失unknown，不invalid非法，也不passed；NULL/空/未知字符串混组不异常、不消失。
3. 新版本每合法路线required dataset missing/degraded/false均阻断；原3条阈值、auction仅自己的显式依赖不变。未支持的合同版本失败关闭。
4. 新闻直接实体/hash/内容和分析可见性失败，即使基础组全ok仍不能进新闻授权；旧sector推断不恢复。K不足/未终场/价格链冲突及竞价逐股门不被全局true覆盖。
5. B/C/D读取同run冻结gate，无map legacy与map存在缺route两种严格区分；较新失败attempt不回退旧成功。全局true、routefalse仍阻断；合法registry增加不扩大消费者。
6. 修复前后旧run/snapshot全字段摘要不变；新run仅追加并记录新quality版本；不从当前数据重算旧概率/排名/门禁。生成完成≠全部route通过，观察候选留证≠交易机会。

本子任务交付仅本文，已把合法路线缺合同与历史不可洗白重点即时报告父；不代实现、不部署、不下单，无后台任务，收尾冻结。

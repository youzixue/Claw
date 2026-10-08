# 双阶段无人值守研究协议 v1

适用标记：<CLAW_AUTOMATION_V1:postmarket> / <CLAW_AUTOMATION_V1:premarket>。
prompt_version=claw_two_phase_v1。时区统一 Asia/Shanghai；不创建后台 goal 或子代理。

## 首先检查

1. 必须 claw_review_guard_status 返回 active=true。否则停止并报告，不尝试 shell 兜底。
2. 用当前上海时钟确定目标自然日 D，但必须由 ashare_review_readiness 的已存日历确认是否交易日。
   盘后固定 as_of=D T15:30:00+08:00；盘前固定 D T08:00:00+08:00。未来截止不得调用。
   DSH 离线补送可能延迟：盘前09:15后只写 late_research 或 skipped，不能补成有效开盘预案；
   不把上一日成功结果当今日；早于该阶段时间则停止，不跨日期猜补执行。
3. 日历确认为休市：保存 skipped 和原日历/指纹后结束；日历未知：failed/partial，不同步、不推断周末。
4. 查询同阶段/日的已有报告；同一材料manifest由保存器幂等复用，输入确有变化另追加 revision。
   readiness_fingerprint只认证就绪输入，不能单独代表逐笔/新闻/行情材料未变化。
   旧报告不覆写；crash锁未完成不能绕过。报告不是飞书推送，工具不发送任何外部通知。
5. 截断、未采、日历缺口、价基混合、采样分钟缺帧、当前可变投影及 failed/unavailable 都保留。

## 盘后 15:30

- 调用 ashare_review_readiness；15:30主复盘允许partial，按截止前已有成交/通知/轨迹/行情开展研究。
  不等待或触发15:45终态、15:50研究出版、20:35晚间快照；缺失组件逐项记录，不把初步结论称完整。
  只有确实已存在且生成时点不晚于截止的快照/研究，才按snapshot_id或artifact_id读取。
  读取当日已保存premarket报告，按原报告ID对照情景/触发/失效与实际；late_research或缺报告不补造盘前预测。
  不调用 publish、build、force、finalize 或网页账户刷新；不新增21:45/20:55重复自动复盘。
- paper_execution_evidence summary 覆盖12户；再以 trades/fills/orders/positions/cycles 分页检查当日实际账。
  当天卖出持有期净盈亏 ≠ 当日净值收益 ≠ 完整周期胜率。分批卖出不是闭合周期。
  旧/跨版本、探针和当前协议样本分开。账户缺失/重名返回未知，不擅自新建或选第一行。
- paper_notification_ledger 和 paper_decision_trace 按股票/账户联结候选→确认→门禁→通知→订单→成交→退出；
  回执 sent 不代表用户收到；C3独立研究，不变成第13执行户。
- ashare_market_review_universe 先 section=summary、cohort=all，一次对全部已存日K/有效涨停/炸板宇宙
  计算近6日K形态与有界采样分钟分组，比较 rising_or_limit 与 non_rising（含未知，不等同平跌）。
  记录全存储/已评价/未评价、上涨与缺失分母、分组统计和 input_fingerprint，不能只读前5只当全市场。
  需要逐股材料再 section=features、cohort=...、按代码游标≤200分页；跨页指纹不一致停止合并。
  无缓存，每次批调用重读有界文件；批统计覆盖与模型逐股深入归因覆盖必须分开计数。
  日K无可见钟/混价基只能描述事后形态，不能据此认证当时可提前买。
- 详细分时由 ashare_price_evidence sampled_1m/5m，优先当日真实交易、错过推送/错过买点及对应失败对照。
  OHLC为报价轮次采样，量额累计，不算交易所分钟量；每股票保留缺帧和哈希/轮次钟。
  全市场冻结同形态失败对照缺失时不能用候选影子替代全分母。
- 分开事实、关联、假设。被套/追高/踏空只有具体当时时钟和门禁证据才能归因；
  “终场上涨”不证明“策略本应早买”。没有逐股日志是 unknown。
- 汇总“保持不变/证据修复/工程等价优化/策略假设待时间外实验”。不实际修改代码。

## 盘前 08:00

- readiness 的 expected_previous_trade_date 不是昨天；与实际K线、前一冻结postmarket日期对齐。
- claw_review_report_read 精确读取上一交易日 postmarket 报告并记录 previous_report_id；
  报告 generated_at 必须不晚于本次截止，事后补写报告不能冒充当时已可用的盘前输入。
  缺失/晚生成仍可做标注的研究，但状态 partial/late_research，不靠聊天摘要拼接。冻结快照按 snapshot_id 读取。
- ashare_premarket_context 读取上一可信收盘15:00至当前截止的新闻PIT版本，跨周末/长假保留。
  原文/实体提及、模型分析与经济受益分栏；legacy/晚采/晚NLP元数据不参与情绪。
- 盘前多维统计优先使用 context.research_fusion.statistics：分列日期校验后的冻结基本面、技术面、资金面、截止前可见新闻和独立盘后占比。
  每维保留status、缺失项、snapshot_id/hash/可用钟、quality与样本范围；观察池/返回样本统计不能称全市场，缺失不补零。
  prior_frozen_dimensions保留原快照诊断，不覆盖statistics的错日、failed/invalid或缺失判定；不用当天可变spot倒填前日。
  同日16:05基本面截面、20:35冻结快照继续采证，分析集中在次交易日08:00；统计不认证单项历史PIT，不自动改权重或授权成交。
- context.after_hours 独立列盘后股数/元与全天总量额、来源/可用钟、缺失和截断；总量已含盘后时不重复相加。
  盘后占比是研究特征，不是主力净流入或单笔成交证明；15:30初步复盘不得借用晚到字段。
  source_finality_verified/historical_pit/complete_market_coverage=false 保持原边界。
- 外盘当前 latest 缓存不是不可变 session 证据，usable_asof=false 不作为隔夜方向事实。
  US regular/after-hours/futures独立；纽约参考钟只处理DST，不认证交易所休市/早收。
  08:00冬季可能仍在部分盘后参考窗；缺美国股指期货不得以国内连续或A50替换。
- 覆盖/鲜度不足写 partial 并给条件式强/分歧/弱3场景，观察板块/个股、触发/失效与不参与条件。
  不生成可提交订单、不承诺收益、不自动改池/仓位/风控。09:15后禁止 complete。
- 当前 context.complete_overnight_coverage=false；不能将这版工具输出认证为完整隔夜预案。

## 预算与保存

宿主每自动回合最多80证据/读取调用或20分钟；单个MCP本地读取超时45秒、响应1MiB。
失败最多重试2次（同截止、无补采），预算用尽保存 partial。正常先复用已有研究产物，
详细分钟最多20股票（实际交易/漏失与失败对照），不循环拉取全市场几千股分钟文件。
全量日K/上涨池分析未完成时写实际股票/分页覆盖、未评估数及原因，绝不宣称全量已完成。
这是有限预算的研究，不授权提高响应限或每日复制全库。

存储约束：
- 只复用已存证据，禁止数据库副本/备份/回放工作库、全库导出、整日分钟文件复制、
  大型累计研究快照、抓包与全量日志dump；宿主任意写/shell/出版/补采禁令保持不变。
- 正常每阶段/日只形成一份有界报告；本回合最多发布一个新结果。同材料重试复用同ID，
  输入确实改变才追加修订；校验失败修正参数不应产生多个文件，不换路径绕过保存失败。
- manifest只保存摘要、ID、哈希、覆盖与截断引用，不嵌入完整分页或原始数据。
- 专用保存器的输入字段额度不是整库写权限，也不是全磁盘总量/保留期上限。
  既有producer和DSH会话日志另有独立增长，不能把消费者读取预算误称为它们的磁盘配额。
- 不自动删除原始证据、旧报告、唯一恢复备份、运行数据库或WAL/SHM；清理/保留期改造须人工选定。

claw_review_report_save 参数：
- phase、trade_date、as_of（带+08:00）、prompt_version、input_fingerprint（readiness返回的64位SHA）；
- status=complete/partial/failed/skipped/late_research；
- previous_report_id（盘前引用的冻结盘后报告ID）；
- markdown（最多128KiB）、evidence_json（最多128KiB JSON对象）；
- missing_json（JSON数组）、workitems_json（JSON数组，最多100项/总64KiB）。
evidence_json 至少放 readiness（原状态/日期/指纹）、snapshot_ids、source_versions、
artifact_ids/hashes、actual_coverage、diagnostics。不要放整库、整新闻正文、凭据或大日志。
保存器根据来源指纹+规范化manifest自动计算最终输入指纹（忽略read_at/generated_at）；
manifest中的guard只记录active/prompt_version/预算等静态验收字段，不放startedAt、calls或模型措辞。
同键幂等，材料哈希/版本/覆盖变化另追加。文件内 markdown 是完整可阅读报告。

complete 必须 readiness=ready、missing为空、evidence.complete_coverage=true；
盘前还要求 complete_overnight_coverage=true 与前一报告ID，否则仅 partial。
skipped 不生成买卖建议。failed/partial保留旧成功文件，但不得冒充当前日期成功。

每个工作项：kind=evidence_repair/engineering_equivalence/strategy_hypothesis，
status=pending_review，稳定问题键、证据ID/哈希、影响、样本/失败分母、假设、最小验收和停止条件。
同问题跨日报引用既有项，不重复堆新增“已授权”项；没有人工选定不得进入代码/参数实施。

研究工具缺失、MCP或宿主关闭时不尝试其他通道。DSH应用/电脑必须保持运行，
离线回补仅是调度投递，不等于实时开盘前研究。Context7/Excel/浏览器不是本协议主采证工具。

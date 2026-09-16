# 9/14 主力资金消费者工程修复

## 约束与事实

- 已读根 AGENTS.md、.workbuddy/memory/MEMORY.md 的资金修复记录。项目不存在 .memory 目录，实际记忆路径是上述 .workbuddy/memory。
- 源码优先：scheduler.py 2638–2673 **已经读取 FundFlow 并调用 current_main_fund_evidence**。没有重复做“StockSpot→FundFlow”迁移；当天0覆盖不能反推其仍消费腾讯field50。
- 腾讯field50是五档委差（手），并非主力资金；当前合法腾讯资金来自 tencent / tencent_hsfundtab_v1。严格来源版本、金额/占比有限值、源/接收/观测顺序、可见性和 FUND_FLOW_SOURCE_MAX_AGE_SEC 均保留。
- 未触碰 scheduler.py、main.py、news、前端、历史复盘 outputs；未部署、下单、业务调用、联网采集、生产数据库读写。测试仅隔离临时库，新增测试禁止 HTTP。
- 此分支仅修工程语义；不凭单日复盘调策略权重、阈值或评分数值。

## 实际消费链审计

| 消费端 | 核验结果 |
|---|---|
| scheduler 情绪资金合计 | 已是 FundFlow + strict helper；空合格样本聚合0仍需父会话处理，见集成建议 |
| paper 候选/执行前确认 | 已是 load_current_main_fund_map；当前缺失返回None和unknown；真实0保持known但不满足正流入确认。未新增放行路径 |
| spot list/detail | 已是strict helper，但过滤后丢失过期原因；本次同一次读取补拒绝诊断 |
| tenbagger 资金prewarm | 已只读scheduler所有的FundFlow，无在线备用采集；本次补观测行/合格行/拒绝原因统计 |
| 异动扫描、冻结证据、展示投影 | 已有signal/display分离和执行时复验；不复制新实现，回归验证 |
| bull/tenbagger 排行返回 | 当前资金缺失被格式化成0亿/0%；本次改null/unknown，真实0仍数值0 |
| promotion资金研究 | 已无StockSpot回填；dated/historical研究不等同current。未扩展修改 |

## 本次精确文件

1. backend/app/data/main_fund.py
   - 抽出 current_main_fund_status，current_main_fund_evidence复用它作为唯一准入判断。
   - 状态：missing、unsupported_source、invalid_values、unknown、invalid、future、stale、ok；保留严格时钟实现。
   - load_current_main_fund_map可选 diagnostics 字典，同一次 SELECT/autoflush=False 返回每code状态；拒绝金额不进入返回map，不为未来记录重建旧值。指定codes补missing；无codes只统计观测行，不代表股票全市场分母。非法日界cutoff不查询并标invalid_cutoff。
   - 已验收ISO时钟统一规范化为datetime，消除“validator接受字符串而API直接.isoformat()崩溃”的契约缺陷。无时钟重打戳。
2. backend/app/api/v1/spot.py
   - list/detail共同传入diagnostics；main_fund_status缺失保持unknown，其余显式stale/future等。
   - 新增main_fund_reason（缺失为missing）和main_fund_available。
   - 拒绝记录金额仍null，来源时钟不作为可用资金附带；排序仍严格使用合格map，真实零/负值排在未知之前。
3. backend/app/api/v1/tenbagger.py
   - prewarm增加fund_quality：status/reason/decision_at/query_performed/row_count/qualified_count/status_counts，分母明确observed_fund_flow_rows。跨日未查询row_count=null，不能读成“0库存”。
   - 两种排行金额/百分比缺失返回null；增加snapshot_known/unknown、main_fund_purpose=ranking_snapshot、原source_quote_at。600秒评分快照**不声称调用时实时资金**。
   - RANK_SNAPSHOT_VERSION v4→v5，旧missing-as-zero持久化快照不能按新契约重新展示；未删除/改写旧快照。
   - 排行评分输入数值与权重未改。
4. backend/tests/test_main_fund_consumer_repair_20260914.py
   - 缺失、零、负数、非有限值、bool、未知来源、未来、无时钟、逆序时钟、跨日。
   - 收盘后阈值等号与+1微秒：不放松新鲜度。
   - ISO时钟跨spot/paper序列化；同一次读取诊断；spot list/detail排序与状态、paper/prewarm跨消费者一致。
   - 全过期prewarm明确不可用；两种实际排行生成及缓存返回均区分缺失和快照零。
5. 本文档。

## 父会话集成建议（未代改）

### scheduler

- 2638–2673 不需要来源迁移补丁。
- 2690附近当前 `round(sum(main_flows) / 1e8, 2) if main_flows else 0`：对外层必须结合合格样本数/覆盖状态表达unknown，而非解释为实测市场净额0。若数值内部接口不能接受None，不要直接给模型塞None；保留内部占位但对外投影为null并加明确不可用状态。
- 需要拒绝原因时，已有ORM查询结果逐行用 `current_main_fund_status(row, trade_date=today, decision_at=fund_decision_at)` 统计，合格数与合计继续用同一helper。不要再次读取新的时点来解释旧轮次，不要把最新数据回填15:00历史。
- 门槛、StockTag分母、来源版本保持不变。

### 前端

- spot的main_fund_available=false或值null显示“未知”；stale提示过期，而非0。
- 两个rank的主力额/占比null显示“—/未知”，snapshot_known说明“排行生成时资金”，可展示main_fund_source_quote_at；不能标实时或借此确认交易。
- 正常实测0仍显示0，不应用truthy判断。
- prewarm fund_quality.status_counts 是数据诊断，row_count不是全市场覆盖分母；query_performed=false不代表无库存。

## 验证进度

- 首轮7文件回归：235 passed（19.65s）。
- 新增测试最终独立运行：22 passed（2.44s），含实际bull/tenbagger两种排行生成与缓存，以及跨日不查询不能宣称库存0。
- 13文件扩展资金契约回归：503 passed、1 failed（24.88s）。唯一失败是既有 test_fund_order_breakdown.py::test_real_alembic_upgrade_advances_only_isolated_026_schema：测试执行upgrade head却写死期待027_fund_order_breakdown，而当前并行迁移head已为029_news_evidence_versions。没有资金行为断言失败；未代改非所有权测试或news迁移。父会话可将此测试升级目标固定为027以匹配其测试目的，或动态断言head并单独验027字段。
- 统一命令前缀：`PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false /usr/local/bin/python3.11 -B -m pytest ... -q -p no:cacheprovider`。
- 另有python_multipart上游弃用提示；上述迁移测试失败已显式报告，未声称全绿、全量测试或生产验收。
- 仓库级git diff --check返回2，列出的空白位于并行scheduler/前端文件，全部在本分支禁止修改范围，未代修。此分支源码初始为untracked，仓库diff不能当作本分支源码差异证明；已读后局部edit并用隔离测试验证。

## 遗留与边界

- 未核验生产运行源码版本/部署状态、实际9/14源水位，故不声称当天资金恢复，也不把0覆盖归因为某一个原因；15:00以后严格过期是可能解释而非生产事实。
- 排行600秒评分缓存是生成时研究快照，不改为实时评分。paper既有执行前资金helper复核继续负责交易门控。
- 研究口径另有待审计：tenbagger排行将最近10个自然日sum命名为5d，_load_recent_main_fund_map有缺值转零。这涉及历史窗口/评分，已向父会话报告，不在本轮语义修复里广泛重构。
- paper对外known/unknown仍是既有保守二态；unknown原因已明确包括缺失/过期/不合格，不伪称0。需要更细拒绝分布可利用本次可选diagnostics集成，但不应改变候选/交易闸门。

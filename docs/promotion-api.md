# 晋级预测接口说明

## 目标

`/api/v1/promotion` 现在按分赛道口径返回数据：

- 首板赛道：`次日首板概率` 为主口径，同时保留 `5日内首板概率` 作为中短期观察子概率
- 二板赛道：`次日晋级二板概率`

这两条赛道不再建议混排解释。前端主展示和导出应优先使用分赛道字段。

## 候选池接口

接口：`GET /api/v1/promotion/candidates`

关键参数：

- `limit`：正式展示区的条数上限
- `ranked_limit`：首板宽召回榜请求深度，上限 30；正式首板审计短名单始终最多 12
- `force_refresh` / `compact`：只影响页面读视图。公共 GET 不接受也不信任 `snapshot_source`、`snapshot_context`，永远不能写入 `schedule` 正式台账；正式 15:10/20:00/09:25/09:35/10:00/10:30/13:05 快照只能由内部调度器在对应时间窗内、携带质量闸门结果生成。

主字段：

- `first_board_candidates`：首板冲刺候选
- `limit_up_platform_candidates`：板后贴板横盘预备池。最近真实涨停后仍贴着涨停锚点/前高附近横盘的个股，优先看十字星和再点火结构
- `limit_up_platform_overview`：板后贴板横盘预备池总览，说明当前有多少只这类预备股、多少只已命中板附近十字星、多少只已进入首板冲刺
- `limit_up_platform_diagnostics`：贴板横盘未入池诊断，只聚焦“离板锚点仍偏远 / 还差十字星确认 / 还差量窒息”三类缺口
- `first_board_watch_candidates`：首板预测梯队。表示未进 1-2 日冲刺主池，但仍保留在 3-5 日首板预测层继续跟踪
- `first_board_watch_overview`：首板预测梯队总览，说明当前以哪一类盘感分层为主、多少只接近主升浪启动区
- `first_board_watch_groups`：首板预测梯队分层，当前按“平台二次点火 / 主线补涨 / 静默蓄势”归组
- `first_board_diagnostics`：首板未入池诊断，按主因汇总被挡掉的票
- `second_board_candidates`：二板候选
- `ranked_first_board_candidates`：正式首板 Top12 精排短名单。按全局时点可见证据排序，不用赛道配额、未确认数量或板块暴露硬删预测；Top12 不等于市场首板总数
- `ranked_first_board_recall_candidates`：首板 Top30 宽召回榜。正式 Top12 是稳定前缀，13-30 名按时间外验证的时序概率补充，仅用于跟踪和复盘
- `formal_first_board_limit` / `recall_first_board_limit`：明确返回正式短名单与宽召回层的口径上限
- `ranked_second_board_candidates`：二板概率榜。二板口径会在当日首板池基础上尝试查询同日龙虎榜成员，按净买额、机构/北向/营业部席位结构对次日连板概率做保守修正
- `prediction_news_end_time`：本批候选实际使用的消息硬截止时刻，用于审计迟到重跑是否发生信息穿越。新闻现在复用不可变内容/分析版本及原文确定性实体验证；仅有可变 `FinanceNews` 的旧记录不能作为时点合格催化。
- `probability_factors.news_evidence` / `reason_snapshot.news_evidence`：本次共享新闻证据引用及截止时点，仅追踪和解释，不补旧预测、不改变评分公式。雷达龙头结果及预案 `main_wave_stats` 沿用同一新闻证据；空对象表示没有冻结证明，不是全市场零新闻。
- `prediction_semantics.news_evidence_gate`：新闻反推板块成分股旁路目前 `sector_inference_status=blocked`、`sector_inference_reason=sector_context_unversioned`；原因是板块强度/成员/行情缺少完整时点版本，不能用当前强度扩散回历史候选。其他独立主线/竞价路线不因此调整阈值。源码改动仍需配套029新闻证据迁移及后端部署验收。
- `prediction_health`：预测快照健康检查。接口会在本次请求写入新预测记录之前，按“交易日 + 赛道 + 独立快照上下文 + 模型版本 + 唯一个股”核对 `promotion_prediction_record`；旧模型版本返回 `stale_model_version`，不能把旧概率当成当前预测。其 `auction_health` 只统计同一交易日每股最终帧，并分别检查 09:24 后时点覆盖和增量成交字段完整率
- `prediction_health.persistence`：内部正式调度本次持久化诊断，区分 `recorded`、`preserved_existing`、`empty_unproven`、`fully_filtered`、`invalid_candidate_identity`，保留适配器收到的输入/过滤/有效身份计数及 `ledger_recorded`。内部生成器携带独立 `ScheduleBatch` 时，空/全过滤/部分身份缺失会追加 `status=blocked`、零快照、`gate_passed=false` 的不可变尝试；即使质量闸门原先通过，也不能覆盖该阻断。其 `reference_trade_date` 是本次调度信号日，旧候选证据日期仅留元信息，不用陈旧日期使阻断被漏选。B/C/D只在各自允许时段与信号日范围内阻止回退旧候选，有效后续新批次可恢复读取。没有独立调度标识的旧调用只返回未落账诊断，公共只读GET不发起正式写入。阻断记录不是完整零候选证明，不能据此声称市场没有机会。
- `generation_attempt`：正式调度确认交易日且仍在既有时间窗后，在独立事务中先追加并提交 `generation_pending` 阻断记录，再开始竞价/收盘检查、新闻缓存检查、质量审计及候选生成。此时输入计数为未知 `null`，不是零；如果后续失败、超时、取消或进程退出，已提交阻断仍有效。后续成功运行保留独立微秒时钟/批次键，避免秒级展示元数据让成功批次反而排序在开始阻断之前；原阻断不改写。候选时钟/上下文必须与成功批次身份一致，校验在兼容记录删除前完成。已成功提交之后仅影子评估超时，不撤销生产批次或另加更晚阻断。日历确认前或阻断事务本身失败仍不能保证已落账；禁止因此开始候选计算，也不伪造持久化成功。
- `actual_limit_up_replay`：实际涨停回放。用最新涨停日的真实首板/二板，对照上一交易日预测记录，逐只标记 `predicted_hit`、`not_in_pool`、`score_low`、`rank_cutoff` 或 `filtered`；汇总会把前收盘预测命中与各盘中确认批次命中分开
- 只读缺口分析：`python3 scripts/analyze_recall_gap.py --database claw.db --days 10`，把近 N 日实际首/二板与前一日正式主榜逐日对比，输出 `outputs/recall_gap_<date>.md`，仅用于提出召回覆盖假设，不作为生产改动依据

调试字段：

- `debug.mixed_ranked_candidates`：首板/二板混排后的联调视图，仅用于排查排序逻辑
- `debug.notes`：当前调试字段的使用说明

`first_board_diagnostics` 重点子字段：

- `blocked_total`：被主筛选挡掉的首板样本数
- `overview`：一句话总览，说明今天首板主要死在什么，以及最该先补的阈值口子
- `reason_summary`：按主因汇总的数量
- `blocked_reason_groups`：分组后的样例明细
- `blocked_reason_groups[].playbook`：这一类票的共性改进建议
- `blocked_reason_groups[].examples[].next_threshold_hint`：当前最优先补的一项阈值
- `blocked_reason_groups[].examples[].actionable_suggestions`：完整建议列表
- `notes`：诊断口径说明

`limit_up_platform_diagnostics` 重点子字段：

- `blocked_total`：还没进入贴板横盘预备池的样本数
- `overview.headline`：一句话说明当前最常见的贴板池缺口
- `reason_summary`：按“离板锚点 / 十字星 / 量窒息”汇总的数量和占比
- `blocked_reason_groups[].playbook`：这一类贴板横盘缺口的共性打法
- `blocked_reason_groups[].examples[].next_threshold_hint`：当前最优先补的一项贴板池门槛
- `blocked_reason_groups[].examples[].actionable_suggestions`：完整建议列表

### 候选响应中的独立次日上涨研究榜

`GET /api/v1/promotion/candidates` 新增 `direction_research`，仅用于“次日上涨研究榜Top12”，不是另一个下单接口，也不覆盖原首/二板表或导出：

- `version`：研究排名合同版本；`label_version='next_day_close_up_v1'`；`scope='research_only'`。
- `rank_limit=12`、`target_precision=0.8`；`candidate_count` / `eligible_count` / `missing_probability_count` / `selected_count` 分别报告候选、合资格、缺方向概率及已选数量。候选数量不等于时间外验证样本数。
- `status` 为 `available` / `blocked` / `insufficient_candidates` / `unavailable`，`reason` 报告阻塞或不可用原因，`notes[]` 保留研究限制。`available` 仅表示榜单可展示，不表示模型验证通过或80%达标。
- `candidates[]` 保留原候选字段及 `probability_factors.direction_research` 证据。服务端已按独立 `direction_probability` 降序、股票代码稳定并列排序；前端保持原顺序，不用涨停概率重新排名，不过滤缺失记录来美化榜单。
- 个股证据的 `rank_position` 是研究序号，`probability_method` 是原方向概率方法；`neutral_prior_insufficient_sample` 明确为样本不足，未知方法显示数据不足，不伪造新模型概率。`probability` 是冻结方向概率证据，`eligible` / `selected` / `rank_contract_complete` / `error` 保留排名资格与完整性审计。
- 页面分别显示候选 `direction_probability`（次日收涨）与 `limit_up_probability`（次日首板；兼容原 `probability`），缺失上涨概率不以涨停概率替代。两者均不是交易收益。
- `production_unchanged=true`、`manual_review_eligible=false`：研究观察不是买入建议、人工晋级资格或执行授权；Champion、正式生产榜、执行风控与订单均不变。
- 旧 API 没有此字段、`status=unavailable` 或旧快照缺少冻结研究证据时，页面显示“等待新冻结批次，不补旧榜”，不从原涨停表、当前评分或事后结果回补。阻塞时不展示候选，候选不足时保留服务端已有研究行但不填满12只。响应研究 payload 的 `frozen=false` / `persistence_status=not_verified` 仅表示本次展示尚未认证为已冻结持久化证据；即使有可展示行也不是合格训练材料、已验证新模型或生产升级。

此为本地接入合同，不代表运行服务已经部署；80%仍是未承诺、未认证的研究目标。`frontend/e2e/promotion.directional-review.spec.cjs` 复用现有 Vite（默认 `http://127.0.0.1:5173`），仅在浏览器请求层 mock 新响应；这是隔离契约验证，不代表真实后端接口已升级，也不需要另起服务器或重启后端。

### 离线方向研究 CLI：只读报告，不持久化或晋级

复用 `backend/scripts/train_promotion_challenger.py`，显式选择 `--objective next_day_close_up --dataset-source prediction_snapshots --target-board 1`；原默认 `promotion` 路径保持不变。方向研究**禁止 `--persist`**，只可生成研究报告，不写训练记录、不发布模型、不自动进入影子或替换 Champion。

在 `backend/` 目录、使用项目现有 Python 环境执行。截止时间必须是已实际到达的 Asia/Shanghai 本地时间（无时区后缀），不能填未来时间。下面在运行前读取真实当前钟作为冻结 cutoff；这是使用示例，不表示已运行或已有合格材料：

```bash
AS_OF="$(TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S')"
python3 scripts/train_promotion_challenger.py \
  --objective next_day_close_up \
  --dataset-source prediction_snapshots \
  --target-board 1 \
  --as-of "$AS_OF"
```

若要使用 `--as-of 2026-09-10T20:00:00`，必须等实际北京时间达到该时刻；不能提前执行，也不能以预期晚间会有数据代替 cutoff 前已知证据。

- 数据复用 `ledger_dataset` 原有整批完整性、记录交易日历真实 T+1、历史身份、封存两日收盘及其他质量门。先校验完整冻结候选池，再仅以冻结的 `prediction_rank_eligible` 资格选择方向训练样本，不能用当前资格重筛历史或先删坏样本绕过整批门。只有新冻结的嵌套 `probability_factors.direction_research` 证据可用；旧记录没有该证据仍为 unknown，不按当前概率或后来结局回填。
- 标签 `next_day_close_up_v1` 使用封存的 T+1 收盘相对前收盘是否上涨，不用涨停标签或盘中最高涨幅。参考基线是冻结方向概率，报告名为 `direction_reference_metrics`，不是用涨停模型错标签充当方向 Champion。
- 独立 logit + Platt 校准通过逐交易日 walk-forward 评估，训练、校准与时间外验证窗口隔离且验证窗口不得重叠。原 AP（average precision）、Brier、ECE 与市场风格等离线质量门仍须通过，不能只看 Top12。
- 目标要求同时满足：Top12 上涨精度 ≥80%；以整交易日为重采样单位的 500 次 bootstrap（固定 seed=0），单侧95%下界（5%分位，`top12_precision_lower_95`）≥80%；至少30个独立 OOS 交易日；每个评价日固定完整12只名单；没有被排除的批次；原离线质量门全部通过。不按个股独立抽样，不删坏批次、缩名单或挑日凑80%。不得跳过应有的验证交易日或失败 fold 来抬高胜率；缺失评价与失败窗口必须保留诊断并阻止宣称研究达标。
- `status=insufficient_evidence`、`target_met=null`：材料、有效窗口、完整名单、独立日数或整批覆盖不足；如实保留 `reason` / `diagnostics` / 被排除批次、覆盖及完整性信息，不写成0%或“未上涨”。
- `status=target_not_met`、`target_met=false`：证据条件具备，但精度、bootstrap下界或质量门没有全部达标；如实展示失败指标，不宣称达标。
- `status=research_target_met`、`target_met=true`：仅这个离线研究窗口通过上述目标，仍非前向认证、收益承诺、可成交证明或部署批准。所有结果均保持 `manual_review_eligible=false`、`persisted=false`、`production_unchanged=true`；不能由该状态绕过原治理/执行协议。

### 正式批次只读诊断与逐路线质量合同（9/14修复源码）

`GET /api/v1/promotion/batch-health?trade_date=YYYY-MM-DD` 只SELECT既有正式台账及本地日历，不补批次、不初始化库、不回写旧记录。按原上下文窗口先选最新尝试再验证；失败不能捞更旧成功批次。schema为 `promotion_formal_batch_diagnostics_v2`。

- 新质量审计/run的 `quality_gate.route_contract` 冻结 `promotion_route_quality_v1_20260914` 及逐路线真实数据依赖；15个合法名包含2个原值兼容名，不扩大原B/C/D执行子集。
- `routes[].route_identity` 是当前合法名称解释；`gate_passed/gate_status` 只依据该run冻结证据，缺失仍unknown。合法有候选却缺gate报告 `completed_route_gate_unknown`；真非法/空名称仍invalid，不删分母。
- `recorded_route_contract_version` 缺失时标记 `legacy_unversioned`，不从当前水位重建旧质量。未知版本/损坏声明不能降级兼容放行。
- B/C/D不再用“旧批次只有batch gate=true”代替逐路线证明；缺route门阻断并要求真实新批次，仍禁止回退。旧run已有明确逐路线门按当时位值读取，但不冒充新版本PIT认证。逐股新闻/K线/竞价、概率与交易风险门继续独立生效。
- `invalid_candidate_route` 是过滤前原始正式候选身份校验失败；独立ScheduleBatch可追加阻断，旧证据不动，过滤计数未知时不写成市场零候选。
- 本契约是工程解释/保守消费修复，不调整评分、配额、模型或生产阈值。具体源码测试和部署边界见 `docs/promotion-route-contract-repair-20260914.md`，不可把源码验收当成已有服务上线。

## 个股概率接口

接口：`GET /api/v1/promotion/{code}/probability`

关键参数：

- `target=1`：返回首板口径
- `target=2`：返回二板口径

返回重点：

- `main_probability_name`：当前概率口径名称
- `candidate_route` / `candidate_route_label`：路由结果
- `strategy_lane` / `strategy_lane_label`：首板策略分层。当前区分“消息催化首板”“竞价高开强攻”“主线扩散补涨首板”和保留原算法的“低吸/半路观察池”
- `watch_bucket` / `watch_bucket_label`：首板预测梯队子层
- `main_uptrend_score` / `main_uptrend_label` / `is_main_uptrend_ready`：是否接近主升浪启动区
- `sub_probabilities`：分阶段概率，首板包含 `next_day_rise`、`next_day_strong_rise`、`first_limitup_next_day` 和 `first_limitup_5d`；上涨方向概率与低基准率涨停概率独立校准
- `memory_features`：首波涨停记忆特征
- `funding_main_inflow_pct_3d` / `funding_positive_days_3d` / `funding_preheat_ready`：截至锚点日的三日主力资金改善，不再只看单日净流入
- `primary_industry_name` / `primary_industry_ignition_ready` / `primary_industry_ignition_score`：主营行业独立点火证据，不会被宽泛热门概念覆盖
- `low_base_sector_ignition_ready` / `low_base_sector_ignition_confirmed`：中低位修复活跃与主营行业点火的组合；`confirmed` 还要求三日资金或直接硬消息确认
- `kline_confirmation.launch_*`：120 日位置、5/20/60 日修复、相对 MA20、量比和换手画像
- `dragon_tiger`：龙虎榜成员摘要，仅 `target=2` 使用。包含是否上榜、上榜原因、净买额、买/卖方席位、机构/北向/营业部净额、席位分和 `probability_delta`

## 2026-09-09 资金来源纠错

- 腾讯普通行情字段50是五档委差（手），不是主力净流入；晋级候选不再用StockSpot该历史兼容列兜底，也不拿另一时点的成交额反算主力占比。
- 缺失的主力净额/占比保留unknown；真实0保持0，缺失资金不贡献资金区间确认分。历史FundFlow仍只作原锚点研究，不因此取得盘中有效性。
- 实时资金投影统一读取调度器入库的FundFlow，校验已知来源版本及源/接收/观测时钟；旧缓存或不合格旧行不能代替本次资金。原正式排名、actionable质量门和成交风控不放宽。
- 该项是字段与数据合同修正，不是重训、产物晋级或真实资金接口已恢复的声明。

## 启动前特征口径

- 当前生产身份为 `promotion_v20260829_27_governed`：保留 v26 的 `launch_precursors_v20260828_2_outcome_split` 与 `temporal_logit_v1_blend75_route25` 评分，但统一启用 `first_board_start_or_consecutive_second_board_v2` 标签、时点化市场风格冻结和不可变影子/人工晋级治理；旧标签版本的结果不得混入新在线校准。
- 首板事件概率由 75% 严格 T-1 时序模型与 25% 路线路由后验混合；`temporal_event_probability`、`route_calibrated_probability` 和最终 `probability` 同时保留，便于审计。训练期为 2026-06-09..2026-08-10，验证期为 2026-08-12..2026-08-27。
- 历史 K 线回放只允许在 `StockSpot.updated_at` 恰好等于 T 后第一个数据库可观测交易日时，用 `StockSpot.prev_close` 修复 T 日收盘；T+2/T+3 实时快照不得回填历史特征。
- 历史对照表明“越低越容易涨停”不成立：绝对低位在未涨停对照中更常见。因此低位字段本身不加分，必须同时满足中低位已经修复、量能换手活跃和主营行业点火。
- 个股资金使用最近三/五个可见交易日聚合。三日主力净流入占比合计至少 3%、且至少两日为正，才标记 `funding_preheat_ready`；单日脉冲不能直接升格。
- 主营行业只使用 `industry` 映射生成独立证据；概念板块仍可作为新闻或涨停归因确认，但不会冒充主营行业点火。
- 直接个股消息聚合 `news_before_close_count`、`news_after_close_count`、`news_high_impact_count` 与 `news_repeated_direct`。显式 `news_end_time` 是硬截止；`promotion_1510` / `promotion_2000` 固定到当日 15:10 / 20:00，盘中批次分别固定到当前交易日 09:25 / 09:35 / 10:00 / 10:30 / 13:05，迟到重跑也不能把后续消息回填进旧快照。
- 首板、普通收涨和强涨使用独立留出 lift：低位行业组合可提高首板/强涨排序，但其普通收涨 lift 不足 1，不能拿首板 LR 直接抬高 `next_day_rise`；资金与直接消息只做目标对应的有界 log-odds 微调。
- `trade_ready`、`prediction_actionable` 及仓位建议仍由独立执行闸门决定，任何启动前证据都不能直接生成买点。
- 可复现实验：`python3 scripts/analyze_limit_up_precursors.py --db claw.db --start-date 2026-04-06 --end-date 2026-08-28 --control-ratio 5 --output-prefix outputs/limit_up_precursors_v26_20260406_20260828`。
- 无未来函数首板排名/覆盖回放：`python3 scripts/backtest_first_board_rank_v26.py --db claw.db --start-date 2026-08-12 --end-date 2026-08-27 --training-cutoff 2026-08-10 --scan-dates 7 --output-prefix outputs/first_board_rank_v26_holdout`。
- 当前留出报告 `outputs/first_board_rank_v26_holdout.md`：as-of 违规 0；旧正式榜 4/80（5.00%），v26 Top12 为 12/132（9.09%），Top30 为 17/316（5.38%，命中日 8/11）；时序 AUC 0.6561，混合概率 Brier 0.01755（优于路由后验 0.01776）；严格扫描从 900 槽位 49.54% 召回提升到 2000 槽位 81.04%。

## 历史面板挑战者口径

模型实验室的 `historical_panel` 数据源只用于预训练、因子筛选和候选召回研究，不是冻结的生产 Champion 对照，也不能凭历史回放结果直接进入影子或生产：

- 默认回看 120 个交易日、每日保留 450 个候选；450 是在同一 500 日面板上按预先约定的“优先提高候选池召回，同时 Top12 不退化、全市场 Top30 召回下降不超过 0.2 个百分点”规则，从 300/450/600 三档中选择的折中值。可用 `--historical-candidate-limit` 显式覆盖。
- 特征只使用 T 日及以前的 K 线、资金流与市场截面；标签使用 T+1 的日期生效主板涨跌停阈值和复权日线。因库内没有历史 ST 身份，2026-07-06 改革前 T 或 T+1 落在 4.5%～6.2% 的样本会保守排除并单列 `excluded_ambiguous_st_*`，这只是“不确定样本隔离”，不能表述为已经准确识别历史 ST。
- 候选预筛中的 MA20 距离使用真实的最近 20 日均线，不再用 20 日前收盘价代替。`prefilter_recall` 的分母是所有通过清洗的主板正样本；正式排名同时返回 `candidate_pool_recall` 和更严格的 `full_universe_recall`，不得用池内分母冒充全市场召回。
- 参考概率使用固定 Beta 先验加只含更早交易日结果的 expanding prior；当天标签不会回填当天参考概率。该参考仅是历史预筛启发式，不是生产 Champion。
- 排序模型只在过去窗口拟合，校准尾窗严格早于验证窗；Platt 校准使用稳定的 Newton/回溯求解。`step_days` 必须大于等于 `validation_days`，避免同一时间外样本被多个折重复计数。
- 历史模型产物固定标记为 `pretraining_only`。即使 AP、Brier、Top12/Top30 和风格切片改善，也必须先把同口径特征写入不可变预测快照，再满足至少 30 个独立交易日的影子验收和人工治理流程；绝不能自动替换 Champion。
- 500 日复现实验与限制见 `outputs/promotion_challenger_validation_20260901.md`。

## 20:00 研究特征材料消费（daily_hist_consumer_v1）

- 仅内部 `schedule / promotion_2000` 的**新快照**尝试附加研究 hist 证据；页面缓存、15:10 和盘中批次不读取归档。读取根目录由 `PROMOTION_DAILY_MATERIAL_DIR` 配置，`PROMOTION_DAILY_MATERIAL_READ_TIMEOUT_SEC` 限制只读校验等待。
- 请求开始以现有台账相同的秒精度 `recorded_at` 一次锁定已发布 ready revision；全市场物理 SHA/同口径复算校验放在工作线程，不传 ORM/Session。后续批量绑定只使用同一不可变 handle，不重选新版本、不现场物化、不中途补源。
- 研究值只加在冠军评分、正式/召回排名和交易资格确定之后的持久化记录中，不参与生产推理。缺股、缺组、旧材料日、错时钟及未知源均记录 `probability_factors.daily_hist_research.status=blocked`；健康摘要提供 ready/blocked 分母及原因，不删候选改善覆盖率。
- 当前默认 `ReviewedSourcePolicy` **没有任何审定供应商解析器**，所以正常状态是 `source_protocol_unreviewed`，不扫描旧文件凑 ready。新增消费者不代表已完成自动采集、前向物化或真实源恢复；单靠可变业务表、来源字符串和调用者自报 complete/finality 不能晋级为材料证据。
- `hist_materialization` 保留真实物化钟、源/接收/观测钟、材料 ref/SHA、特征值 SHA 与消费者截止。旧 snapshot/历史 unknown 不更新；原 `FEATURE_VERSION`、冠军版本、生产质量门与原始 T+1/风控不变。研究正式标签及历史 ST 双时态仍须各自合格材料，不能从 hist ready 推导已满足。

### 原始响应捕获（本地显式研究工具，未部署）

- `scripts/capture_source_responses.py` 只复用原东财资金分页、同花顺年线 HTTP 入口；显式 `response_capture` 在解析/日期筛选前保留有界 HTTP 200 entity body，默认生产 source 不启用，不写盘、不新增调度。
- 缓冲记录原页/尝试或代码/年份/尝试及实际接收/观测钟；归档在本次请求结束后放到工作线程，复用 `MaterialArchive` 的原子不可覆盖/SHA/路径保护。HTTPX 解压后的 body 不标成压缩网络原字节，不存 Cookie、请求头或异常消息。
- 这些原件以 `observed_now_transport_unreviewed` 封存，明确无 `source_published_at`、完整宇宙或历史首知证明，绝不写入 ready。年文件包含过去日期不使其成为过去已知证据；捕获失败、预算截断和空响应显式记录，采集返回值与正式质量门不改变。
- CLI 限同花顺1至3个探测代码或原资金全分页，输出到项目 outputs 下新目录。单股探测和本地接口不是全市场定时 producer，也不代表供应商恢复或源协议已审定。

## 不可变 ledger / 影子研究的历史身份门（M2）

- `promotion_identity_mainboard_security_session_v2` 区分事实生效时间与系统实际首知时间：预测身份必须在原正式 Run 的 `as_of_at` 已知，不能用稍后的影子评分钟或今天的 StockTag 补历史；结局身份必须覆盖登记的真实 T+1 竞价、上午、下午和收盘，午间身份往返也不能隐藏。
- 独立追加档案保存 raw / record / receipt 实物及修订链；更正通过 supersedes 追加，不覆盖旧件。证券身份与 ST/退市风险/正式上市状态/停复牌/板块分别验证。一股缺证据或超出普通主板研究 profile，整批留在 unknown 分母，不删坏股改善指标。
- 真 ledger 训练合同为 `promotion_ledger_dataset_v4_sealed_outcomes`，影子评价为 `promotion_shadow_evaluation_v9_sealed_outcomes`；后者增加 `identity_coverage`、逐批拒绝原因及身份材料哈希。最新正式重试先按完整批次确定，失败、空批或缺当前赛道不能回退到较早有利批次。
- 默认身份源解析器仍为空，当前档案功能**不等于历史 ST 来源恢复**；普通主板 profile 也不是全部 IPO/特殊证券交易制度认证。`FEATURE_VERSION`、Champion、生产风控、存量仓位冻结合同不变，缺证据只能继续 collecting，不能训练或晋级成可用前向证明。
- 本节描述本地新增合同，是否激活以单独的原服务部署验收为准；不因代码或单元测试存在而声称已在线。

## 正式结局材料（本地研究合同，未部署）

- `promotion_paired_label_forward_adjusted_prediction_cutoff_v2` 复用 M1 的同一 close/pool raw envelope，只增加独立 `outcome_receipts/ready`，不复制供应商表、不污染 hist ready。发布 receipt 不等于通过最终性/宇宙/时钟检验。
- T日材料必须在原 Run.as_of 前已实际可用；登记的真实T+1材料必须在评价cutoff前可用。读取锁定及整批 gate 校验均放工作线程，不传 ORM。缺原件/坏SHA、缺最终性、未知源、同钟冲突、缺宇宙成员及空池未获证明均整批拒绝，不回退旧有利版本。
- ledger 与 shadow 保留既有日历、原始批次/风控、运维全量检查及个股非正式/停牌/断价/quarantine 门，额外核 sealed 两日事件bool及T close/T+1 close/prev_close与当前读值精确一致；差异不以当前表覆盖材料。最终标签取 sealed 事件，而非无证据的名单缺席。`outcome_material_coverage` 保留未知批次分母，查询墙钟不反复生成同一评价。
- 材料本身须同一已审 forward_adjusted/CNY_per_share、调整版本和 shares/CNY 单位。现有 StockKline 不具备相同不可变单位列，数值一致不等于给旧表补出复权身份。此合同不适用于未复权清仓后收益或独立双源crosscheck。
- 真实审定解析器和前置自动材料生产仍未接通，默认只有 unknown/blocked。不得把本地工程、隔离正向fixture或当前运维ready当作实际标签恢复；不变更 FEATURE_VERSION/Champion，不补历史首知或交易。

## 龙虎榜席位口径

二板候选的龙虎榜增强只作用于“最新交易日首板 → 次日二板”的赛道：

- 先取当日龙虎榜汇总，只有当日上榜个股才继续取买入/卖出席位明细
- 席位明细按“机构专用 / 沪深股通专用 / 普通营业部”做保守归类，不硬编码游资名录
- `dragon_tiger_probability_delta` 只做小幅修正，净买入、机构净买和营业部净买集中会加分；净卖出、机构净卖、卖方集中和高成交占比但净买弱会降权
- 龙虎榜数据不可用或个股未上榜时，概率不做席位修正，接口不应因此报错
- 如果同板块首板在早盘集中封板，二板会增加“早盘板块强化”小幅修正；该修正只覆盖弱首板被板块扩散带动的场景，不直接替代封板质量、龙虎榜和风控判断

## 使用建议

- 页面将 `ranked_first_board_candidates`（正式 Top12）、`ranked_first_board_recall_candidates`（Top30 宽召回）和 `ranked_second_board_candidates` 分层展示；宽召回层不是买入清单
- 首板策略不要混为同一种买点：消息催化、竞价强攻、主线扩散补涨可作为独立冲刺路线；原有形态算法保留为低吸/半路观察池
- 正式竞价强攻路线只接受 `2.6% <= auction_open_change < 6.0%` 且增量成交字段完整；高开达到 6% 的个股仍可作为板块宽度证据，但不进入次日低赔率冲刺榜
- 主板可交易过滤必须在预热 K 线扫描和竞价/异动候选合并前完成，避免创业板、科创板样本占用候选上限
- 复盘命中/漏判时优先看 `actual_limit_up_replay.summary` 和 `actual_limit_up_replay.items[].blocked_reason`，再回看候选池分数和诊断；二板成功必须是“预测日首板 → 下一交易日二板”的连续涨停，普通次日首板不能计作二板晋级

### `GET /api/v1/promotion/learning-review`

逐日对齐前一交易日正式收盘 `schedule` 主榜（15:10 / 20:00 批次）与记录交易日历的真实 T+1 行情，默认回看 10 个交易日，最多 30 日。页面临时补算、竞价和盘中批次不混入该收盘复盘口径；缺少真实 T+1 时不得跳到下一个有行情的日期代替。

- `latest`：最新一天的全市场/主板首二板数、正式预测数、上涨/强涨/涨停命中、精度、召回率、Brier 误差、分赛道成绩和漏选样本
- `aggregate`：回看窗口的加权汇总，不能把逐日百分比简单平均
- `daily`：逐日成绩单；页面补位观察股不计入 `predicted_count`。正式预测精度与 `prediction_actionable=true` 的可执行子集精度分别统计，不能把“预测会涨停”和“当时可以买”混为一个结果
- `lane_metrics.target_1/target_2`：首板和二板结果完全拆开；`pool_hit_count/pool_recall` 表示候选池覆盖，`recall_ranked_count/recall_hit_count/recall_precision/recall_recall` 表示宽召回层表现，不能再把二板命中显示成泛化的“涨停命中”
- `latest.launch_precursor_metrics` 与 `aggregate.launch_precursor_metrics`：对全部首板主榜、中低位修复、三日资金、主营行业点火、低位行业组合、直接高影响/重复消息及持续流出风险分组统计样本、上涨/强涨/首板精度和相对 lift；分组可重叠，`actionable_*` 指标单列
- `recommendation`：根据多日召回、精度和漏选构成给出 `expand_feature_coverage`、`rerank_missed_candidates`、`repair_signal_quality` 等建议
- 模型自动做路线级 beta-binomial 后验校准：以路线真实命中率为中心，仅保留部分个股相对 log-odds 残差，避免将排序分冒充真实概率；对累计至少 8 个真实命中、但主榜召回不足的已入池路线，最多增加 8 分漏选召回排序分。该分只用于重排，不抬高买入概率、不放宽买点阈值
- 盘后 20:20 调度会回填可评估的主榜结果并输出当日精度/召回审计日志
- 页面加载时优先看 `prediction_health.should_warn`：为 `true` 时只把候选池当成临时生成/补算结果，不能解释为盘前已存在的预测快照
- 不要再把 `debug.mixed_ranked_candidates` 当成统一概率榜
- 导出时建议按“首板概率榜 / 二板概率榜”分表处理

### 次日上涨方向复盘：覆盖率与 80% 目标

以下为 `latest`、`daily[]`、`aggregate` 的兼容新增字段；`launch_precursor_metrics` 的 cohort 也可能提供同名字段。旧响应可缺失字段，前端必须显示 `--` / “数据不足”，不得把 null、空字符串、未返回的率或达标状态转成 0% / false。明确返回的数值 0 则是真实零值。

设原正式名单数量为 N、可评价数为 E、未知数为 U、已评价上涨数为 H：

| 字段 | 口径 |
| --- | --- |
| `predicted_count` | 固定原始正式预测名单分母 N；不删除缺失结局个股以凑出 80%。cohort 使用其原分组名单 `sample_count`，若提供 `predicted_count` 则按响应展示 |
| `directional_evaluable_count` | 可在登记真实 T+1 评价上涨方向的数量 E |
| `directional_unknown_count` | 缺失或不可评价结局数量 U；missing outcomes 为 unknown，不当作不涨或命中 |
| `directional_coverage` | E / N，0..1；无分母为 null |
| `directional_observed_precision` | H / E，仅已评价子集；E=0 时 null，不代表全名单成绩 |
| `directional_precision` | 完整原名单的上涨实际率；结局不完整、分母为空时 null，不以子集率补位 |
| `directional_precision_lower_bound` | H / N，固定原名单分母的保守下界 |
| `directional_precision_upper_bound` | (H + U) / N，固定原名单分母的可能上界；不是置信区间 |
| `directional_target_precision` | 0.8，即次日上涨目标 80% |
| `directional_target_met` | bool 或 null；按服务端判定展示，null / 缺失为达标未知，不以观察子集率自行推断 |
| `evaluation_status` / `evaluation_reasons` | 保留 partial / unavailable 等整体状态与缺失原因；预测快照完整不等于结局完整。整体 partial 可能仅来自涨停池缺失或另一赛道，不能据此遮盖方向覆盖完整且服务端已返回的上涨率与目标标记 |

窗口 `aggregate` 按同口径计数加权，保留未知名单分母，不简单平均每日百分比，也不把不可评价日静默删掉。上下界分母为空时均为 null。前端同时展示最新/汇总、逐日及可用 cohort 的原名单、可评价、未知、覆盖、观察子集与上下界。

**四种口径不得混淆**：显示概率是预测时的模型估计；上涨实际率是原名单真实 T+1 的方向结果；涨停命中是独立的次日首板/连续二板事件；交易收益还依赖真实成交、成本、退出和风控，不能从“可执行子集精度”推出收益。

80% 是待验证研究目标，未承诺、未认证；单个样本/窗口标记达到目标不等于模型获得生产认证。该展示与复盘字段不改 Champion、生产排序、执行闸门、订单或持仓。本节是接口与页面合同，不代表服务已部署、真实数据已经恢复或全窗口达标；独立 research-only 方向榜使用上文候选响应中的 `direction_research` 合同，不复用涨停概率冒充方向概率，也不将本节原正式名单复盘当作独立研究榜的已认证成绩。

## 竞价来源证据（auction_provenance_v1）

- 新帧持久化 source/source_version、source_quote_at/received_at/observed_at、price_basis/volume_basis、volume_unit/amount_unit；原始量值不改写，明确 share 或 lot100（100股/手），金额为CNY。历史空列保持unknown。
- 合格帧要求同日、源≤接收≤原观测≤决策，均在09:15～09:25:30；原观测距源时钟最多 `AUCTION_SOURCE_MAX_AGE_SEC=30` 秒。09:25前仅接受明确的indicative_match/indicative_matched，09:25起接受明确的auction_opening/auction_matched。这是接入合同，不代表现有公开源已具备该能力。
- 当前AkShare东财/新浪spot适配器只有今开和累计量额，未证明虚拟匹配量价及完整源日期时钟，故只留观察帧；不能把新浪时分秒补成今天，不能用普通stock_spot或盘后数据回填。
- Collector/质量闸门至少要求同股两个不同源更新时点的合格正量额帧；重复HTTP接收同一源帧不累计。终场鲜度看源时点，不能靠09:24后的接收时间洗白09:24前数据。数值覆盖、接收时间覆盖和来源验证覆盖分列。
- 晋级context增加auction_evidence_status/auction_evidence_contract，并传播到probability_factors；缺字段、旧complete=true均不视为已通过新来源校验。价格集群/翻红可保留预测观察种子，不升级为买点。未知个股竞价因子返回null及状态，不虚造量比1。
- D2继续要求早段、09:20～09:25不可撤单阶段及终场路径，缺来源证据产生evidence_blocked，保留structural_pool。新合同只旋转D路由证据版本，不修改持仓冻结退出参数或其他路线阈值。
- 字段单位依据 [AKShare文档](https://akshare.akfamily.xyz/data/stock/stock.html)；虚拟参考价/匹配量和不可撤单时段依据 [上交所交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)。

## 调度要求

调度器会在以下时间自动生成晋级预测并写入 `promotion_prediction_record`：

- 交易日 15:10：收盘后第一版
- 交易日 20:00：晚间复盘版
- 次交易日 09:25：竞价确认版
- 次交易日 09:35：开盘后确认版
- 次交易日 10:00、10:30：早盘主线扩散刷新版，仅策略 C 额外消费
- 次交易日 13:05：午后开盘主线扩散刷新版，仅策略 C 额外消费

B/C/D 消费不可变批次时，先锁定各策略允许的最新批次，再验证真实可见时点：`as_of_at <= created_at <= completed_at <= 决策截止`；有 QuoteRound 时，截止取本轮 `as_of_at` 与决策时间的较早值。缺失时钟、时钟倒序、未来生成/完成、失败批次均等待，不过滤掉异常批次后向旧榜捞候选；历史批次不补造完成时间。C 的诊断另列预测排名资格与未入全局榜数量，`prediction_rank_eligible=true` 不等于进入正式/召回榜，也不升级 `pool_unranked` 为买单；原有实时确认不变。

竞价数据采集在 09:15-09:25 窗口每30秒轮询，并安排 09:20:06、09:24:00、09:25:06 固定任务；09:26兜底已取消。只保存响应实际观测时钟，超过09:25:30不补造终场快照。各盘中正式运行允许首板候选以当日可见数据为锚、二板候选以上一收盘日为锚，但两个锚都必须严格解析到当前记录会话；更早或未来日期会被拒绝。B/D 仍只消费 09:25/09:35 开盘确认批次，避免盘中重算改变竞价与二板策略口径；C 可消费后续主线刷新批次，并继续受同板块强度、资金、涨停宽度和不追高闸门约束。周末新闻会在周六/周日定时补爬和精洗，周一 07:10 再兜底一次，供消息催化首板路线使用。

已有 Claw 数据库部署新版前须先备份并在隔离副本演练 `python -m alembic upgrade head`，源码当前 head 为 `026_auction_evidence`（不代表运行服务已迁移）。025/026只为资金/竞价增加nullable来源证据列，不回填历史unknown；019～024延续模拟审计、时点质量和QuoteRound迁移。其中 `007` 引入独立快照身份，`008`～`014` 增加质量治理、不可变台账、训练/风格/复盘/影子部署表，`015` 将兼容台账的 `model_version` 扩到 80 字符，`016` 给 `limit_up_pool` 增加软隔离 `quarantined`（脏交易日只隔离不删除），`017` 增加每日基本面截面快照 `stock_fundamental_daily`，`018` 增加 GPT 复盘报告 `review_gpt_report`。注意该仓库的 `001` 是历史 `Base.metadata.create_all` 基线而不是空库建表迁移：全新空库必须先由 `init_db` 创建当前 ORM 结构，再执行 `python -m alembic stamp head`；禁止直接从空库运行 `upgrade head`。

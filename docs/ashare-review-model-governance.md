# A股每日复盘与涨停预测治理

## 目标与边界

本改造把“每天让 GPT 根据当天输赢直接改代码”替换为可复现的研究闭环：

1. 本地确定性代码负责交易日、涨跌停真值、特征、训练、回测、版本和切换。
2. GPT/Skill/MCP 负责证据编排、复盘、假设生成和代码审查，不凭记忆补全 A 股事实。
3. 每日自动追加数据、快照、标签、归因和影子证据，但不自动改生产权重、不自动晋级模型。
4. 历史数据用于预训练和因子筛选；只有冻结的正式预测快照能提供 Champion/Challenger 晋级证据。
5. 本系统是研究与决策支持工具，不承诺涨停命中或交易收益。

## 已实现的数据链路

### 1. 统一真值与时点

- `StockKline` 是交易日和完整结果 horizon 的权威时钟。
- `LimitUpPool` 是涨停事件真值；交易日不在权威 K 线日历的脏记录标记 `quarantined=true` 软隔离，质量审计与预测加载均跳过，绝不物理删除；首板标签要求预测日未涨停且下一有效交易日涨停，二板标签要求两个有效交易日连续涨停，统一使用 `first_board_start_or_consecutive_second_board_v2` 口径。
- 盘前/盘中/盘后三阶段都保存 `analysis_trade_date`、`as_of_at`、数据版本和质量状态；基本面维度优先读取当日截面快照 `stock_fundamental_daily`，缺失时明确 `unavailable_for_historical_snapshot`，不用当前截面冒充历史。
- 高标学习统计在复盘的快照中给出：上一交易日连板今日晋级率、断板率、昨日涨停/前连板今日平均溢价。
- 不完整候选快照不得把真实涨停自动归因为“未入池”。
- GPT 复盘报告写入 `review_gpt_report`；默认关闭，需 `REVIEW_GPT_ENABLED=true` 与 `REVIEW_GPT_CLI`，且只读，不修改任何模型权重。

### 2. 不可变预测与实验账本

- 正式 schedule 预测写入 `promotion_prediction_run` 与 `promotion_prediction_snapshot`。
- 训练运行、模型产物、实验、影子运行、影子分数、影子评估和部署事件均有独立审计表。
- 正式预测、影子证据和人工部署事件禁止更新或删除；新结果只能追加新版本/事件。

### 3. 防泄漏训练

- 特征契约只接受预测时点可见字段。
- 训练采用按交易日展开的 walk-forward，拟合、校准尾窗和验证窗严格按时间隔离。
- 使用低基准率指标：PR-AUC、Brier、log loss、ECE、每日 Top-5/12/30 精度与召回。
- 市场风格按相同时点分类，并分别报告退潮、修复、板块轮动、板块主升、个股主升、高标投机、普涨趋势和均衡震荡切片。
- 离线 walk-forward 留出窗（包括当前训练运行采用的 12 个留出交易日）只用于判定产物是否具备进入影子的资格；它与产物冻结后未来发生的在线影子期是两段独立证据，不能折抵下文至少 30 个影子交易日的要求。

### 4. 每日复盘工作台

页面 `/daily-review` 提供：

- 盘前、盘中、盘后不可变复盘快照；08:45 盘前复盘引用上一交易日 `promotion_2000`，11:35 盘中复盘引用当日 `promotion_0935`，盘后复盘引用当日 `promotion_2000`，避免在 09:25 之前误用尚未发生的竞价快照。
- 资金面、消息面、基本面、技术面四维证据。
- 市场风格、涨停池、高标形态和次日条件预案。
- 命中、交易门禁拦截、排序漏失、候选召回漏失和主榜误报归因。
- 追加式人工笔记、自动运行记录、告警和日期区间安全回放。

### 5. 模型实验室

页面 `/model-lab` 提供：

- 历史 K 线面板预训练与冻结预测快照晋级验收。
- 市场风格快照和分风格评估。
- 模型产物、训练记录和不可变预测运行。
- 同一候选集的影子运行、累计影子验收、人工晋级和回滚审计。

## 影子运行契约

1. 产物必须由冻结预测快照训练并通过离线闸门，状态为 `shadow_eligible`。
2. 预测运行必须是正式 schedule 快照，时点上下文必须与训练契约相同。
3. 预测日必须晚于产物拟合及校准数据的最后日期。
4. Challenger 只能对 Champion 的同一候选集合打分，不能借此扩大/缩小样本。
5. 输入交易日必须把数据质量闸门写入同一不可变 `PredictionRun` 且结果为通过；结果日必须在 15:10 后，K 线数量同时满足绝对下限和相对近 60 日峰值至少 95%（可由治理配置收紧），且具备可验证涨停池；盘中数据、部分 K 线和未验证的空涨停池一律不结算。
6. 挑战者的 Top5/12/30 只能在冻结生产 `rank_eligible` 集合内重排；全池 AP/Brier 仍保留所有候选，漏选正样本继续进入召回率分母。
7. 每个交易日必须携带完整排名契约（版本、候选资格、正式榜/宽召回上限及连续唯一排名）；缺契约或用 Top12 回退冒充 Top30 时只能 `collecting`。
8. 同一日期/上下文发生重试时，累计验收只采用最后一个不可变正式运行。
9. 累计验收至少需要 30 个影子交易日、50 个正样本和 2 个可评估市场风格；按交易日成组做 500 次确定性配对 bootstrap，PR-AUC、Brier 改善和 Top12 精度增益的 90% 置信下界均不得为负。
10. 影子阶段永不改写生产输出，也不生成订单。

影子决策只有三种：

- `collecting`：交易日、正样本、风格覆盖或输入质量证据不足。
- `shadow_rejected`：样本已经足够，但性能、校准或风格稳定性闸门失败。
- `manual_review_eligible`：全部闸门通过，只代表可进入人工审批。

## 人工晋级与回滚

### 晋级

人工晋级必须同时满足：

- 最新累计影子评估为 `manual_review_eligible`。
- 产物、注册版本和本地 JSON 完整一致；同一模型版本的产物文件只创建一次、禁止覆盖，影子运行与审批事件逐次校验 SHA-256。
- 后端必须配置 `PROMOTION_GOVERNANCE_TOKEN` 和 `PROMOTION_GOVERNANCE_OPERATOR`；浏览器只在当前页面内存持有令牌，并通过 `X-Claw-Governance-Token` 请求头提交。令牌未配置、缺失或错误时写接口 fail closed。
- 操作主体从后端配置派生，不信任请求体自报的 `operator`；不少于 10 个字符的理由必须记录。
- 每次操作携带稳定 `operation_id` 和 `expected_current_event_id`：重复请求幂等返回，过期页面产生的并发写入由 compare-and-set 拒绝。
- 精确输入确认短语 `APPROVE_CHAMPION`。确认短语只防误触，不替代授权令牌。

审批后只让 Challenger 概率主导**已经通过候选召回和交易性过滤**的分赛道排序；候选生成、风险、流动性、交易门禁和可执行性仍独立生效。生产候选必须携带同一次冻结的市场风格快照 ID、版本、数据版本和时点，缺失、不一致或来自未来时点时立即回退 Legacy。首板和二板分别部署，互不隐式联动。

部署事件每次读取时都会重新校验产物字节、标签版本、影子评估版本和当前完整验收策略。任一证据过期、被篡改或不再匹配时，该赛道立即 fail-closed 回退 Legacy；必须用当前契约重新积累影子证据并人工审批，旧审批不会自动复活。回滚到上一人工版本前也执行同一校验，校验失败则直接回退 Legacy。

### 回滚

- 精确输入确认短语 `ROLLBACK_CHAMPION`。
- 追加新回滚事件，不修改原审批和影子证据；事件保存明确前驱，权威顺序使用自增事件 ID，不依赖墙钟。
- 相同 `operation_id` 重试不会继续多退一层；只有新的操作 ID 和匹配的 `expected_current_event_id` 才能执行下一次回滚。
- 优先恢复上一人工版本；无上一版本时恢复基础 Legacy Champion。多级 A→B→C 会按 C→B→A→Legacy 保留完整链路。
- `PROMOTION_DEPLOYED_OVERLAY_ENABLED=false` 是紧急执行开关；关闭后即使存在审批事件也回退基础逻辑。

## 自动化

默认调度：

- 08:45 盘前复盘。
- 11:35 盘中复盘。
- 20:10 市场风格快照。
- 20:35 盘后复盘。
- 正式预测生成后，对所有兼容的 `shadow_eligible` 产物自动运行影子打分和可用结果结算。

自动流程具备幂等键、有限重试、陈旧运行恢复和持久化告警。自动流程不得调用人工晋级或回滚接口。

旧版按单日/短窗口表现直接修改生产类变量的反馈环由 `PROMOTION_LEGACY_DAILY_WEIGHT_ADJUSTMENT_ENABLED=false` 默认关闭。

## Skill 与只读 MCP

项目 Skill：`.dsh/skills/ashare-daily-review/SKILL.md`。

只读 MCP：`backend/scripts/ashare_review_mcp.py`。它只代理 GET 接口，提供复盘、归因、市场风格、预测运行、训练运行、影子运行、影子评估、部署审计和数据质量读取；不提供训练、审批、回滚、改参、笔记或下单工具。

新会话可通过 `claw_ashare` 读取证据。若本地 API 不可用，Skill 必须报告缺失，不得用未经治理的网页数据伪造本地真值。

## 主要 API

- `GET /api/v1/model-lab/identity`
- `POST /api/v1/model-lab/train`
- `GET /api/v1/model-lab/runs`
- `GET /api/v1/model-lab/artifacts`
- `POST /api/v1/model-lab/shadow/run`
- `POST /api/v1/model-lab/shadow/evaluate`
- `GET /api/v1/model-lab/shadow-runs`
- `GET /api/v1/model-lab/shadow-evaluations`
- `GET /api/v1/model-lab/deployments`
- `POST /api/v1/model-lab/deployments/approve`
- `POST /api/v1/model-lab/deployments/rollback`
- `POST /api/v1/daily-review/replay`（默认只读演练）

## 推荐运行顺序

1. 先运行数据质量审计，修复交易日、K 线、涨停池和快照时点问题。
2. 用历史面板做预训练/因子筛选，不据此晋级。
3. 积累 `promotion_2000` 冻结正式预测快照。
4. 用冻结快照训练；离线闸门通过后登记 `shadow_eligible` 产物。
5. 至少积累策略规定的影子交易日、正样本和多个可评估市场风格。
6. 在模型实验室比较同池 Champion/Challenger 指标与逐风格表现。
7. 只有证据为 `manual_review_eligible` 时进行人工审批。
8. 部署后持续影子监控；数据、校准、风格稳定性或执行风险恶化时人工回滚。

## 自测

```bash
cd backend
python3 -m pytest -q
python3 scripts/ashare_review_mcp.py --self-test

cd ../frontend
npm run build
```

Skill 校验：

```bash
python3 /Users/youzix/.deepseek-harness/codex/skills/.system/skill-creator/scripts/quick_validate.py \
  /Users/youzix/WorkBuddy/Claw/.dsh/skills/ashare-daily-review
```

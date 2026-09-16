# A股数据与标签契约

## 时点

- `premarket`：只使用目标交易日前一有效交易日收盘数据，以及 `as_of_at` 前发布的消息/竞价。
- `intraday`：保留盘前原预测，只追加盘中确认；不得使用尚未形成的当日收盘 K 线作为特征。
- `postmarket`：使用当日收盘前可见数据生成新快照；不得反写盘前或盘中快照。
- 交易日以本地 `StockKline` 的有效日期为市场时钟，并排除官方休市日。周末规则不能替代春节、国庆等长假日历。

## 涨跌停真值

- 统一使用 `app/core/price_limit_rules.py` 的日期感知、板块感知规则。
- 主板普通股、创业板/科创板、北交所和 ST 的价格限制不同；历史 ST 身份未知时，5% 附近样本标为歧义并排除，不强行贴负标签。
- 训练标签优先使用有效交易日上的 `LimitUpPool`，并与 `StockKline` 日期相交。旧 `outcome_status` 只作质量诊断，不作真值。
- 次日首板：预测日非涨停且下一有效交易日涨停。首板晋二板：预测日与下一有效交易日连续涨停。统一标签版本为 `first_board_start_or_consecutive_second_board_v2`，旧版本结果不得混入新模型训练或在线校准。

## 数据质量

- 每次输出都报告数据版本、快照时点、股票覆盖数、关键源状态和质量告警。
- 预测快照低于最低候选数时，状态为 `snapshot_incomplete`；实际涨停未出现在残缺快照中不能计为 `not_in_pool`。
- 当前截面估值或增长字段不能冒充历史基本面。历史不可得时明确写 `unavailable`。
- 新闻必须满足 `publish_time <= as_of_at`；GPT 不得用训练语料中的旧闻补齐本地缺口。

## 风格标签

使用版本化、可解释的状态作为上下文和分层指标，不作为每日自动改参开关：

- `risk_off`：退潮/风险规避
- `recovery`：冰点修复
- `sector_rotation`：板块轮动
- `sector_maintrend`：板块主升
- `individual_maintrend`：个股独立主升
- `high_board_speculation`：高标投机
- `broad_trend`：普涨趋势
- `balanced`：均衡震荡

风格置信度低或数据质量为 `partial` 时，同时展示主、次风格，不作强断言。

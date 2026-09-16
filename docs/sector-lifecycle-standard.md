# 板块生命周期判定标准表

## 1. 目标

统一板块营地生命周期状态的判定口径，明确：

- 使用哪些字段
- 每个字段的业务含义
- 每个状态的判定阈值
- 判定优先级
- 状态流转规则
- 当前系统现状与建议落地方向

本文档用于统一：

- 后端实时/盘后生命周期计算
- API 返回口径
- 前端状态展示与说明
- 后续测试样例与回归基线

---

## 2. 生命周期状态定义

系统统一使用以下 7 个状态：

| 状态码 | 中文名 | 业务定义 |
| --- | --- | --- |
| `emerging` | 刚启动 | 板块开始活跃，首板/资金/指数趋势开始出现，但梯队尚未完全形成 |
| `accelerating` | 加速中 | 连板梯队形成，资金持续流入，板块结构加强，具备主线扩散特征 |
| `climax` | 高潮 | 批量涨停，高标很高，封板极强，市场关注度极高，但拥挤度也高 |
| `diverging` | 分化 | 龙头仍有强度，但后排掉队，晋级率下降，炸板/回落增多 |
| `declining` | 退潮 | 龙头断板或板块结构瓦解，资金明显流出，指数技术面走弱 |
| `one_day` | 一日游 | 前一日启动但次日无持续、无溢价、无扩散 |
| `dormant` | 休眠 | 无涨停、无资金、无趋势、无赚钱效应 |

---

## 3. 判定字段清单

### 3.1 必需字段

| 字段 | 来源 | 含义 | 是否主维度 |
| --- | --- | --- | --- |
| `limit_up_count` | 涨停池 + 板块映射 | 当日板块涨停家数 | 是 |
| `first_board_count` | 涨停池 | 首板家数 | 是 |
| `consecutive_board_count` | 涨停池 | 连板家数（2板及以上） | 是 |
| `max_board_height` | 涨停池 | 板块内最高连板高度 | 是 |
| `fund_flow` | 板块资金流 | 当日主力净流入（亿） | 是 |
| `fund_flow_3d` | 板块资金流 | 近 3 日累计净流入（亿） | 是 |
| `active_days` | 生命周期表 | 当前周期连续活跃天数 | 是 |
| `prev_state` | 生命周期表 | 前一交易日状态 | 是 |
| `change_pct` | 板块实时/收盘行情 | 板块当日涨跌幅 | 是 |
| `kline_trend` | SectorKline | 板块趋势状态（up/down/breakout/breakdown） | 是 |
| `kline_close` | SectorKline | 板块收盘指数 | 是 |
| `kline_ma5` | SectorKline | 5 日均线 | 是 |
| `kline_ma20` | SectorKline | 20 日均线 | 是 |

### 3.2 强烈建议补齐字段

| 字段 | 来源建议 | 含义 | 用途 |
| --- | --- | --- | --- |
| `limit_down_count` | 跌停池 + 板块映射 | 板块跌停家数 | 退潮确认 |
| `seal_rate` | 涨停池 | 封板率 | 高潮 / 分化 |
| `blowup_rate` | 涨停池 + 炸板数据 | 炸板率 | 分化 / 退潮 |
| `promotion_rate` | 梯队演化 | 晋级率（昨日首板/二板今日晋级） | 分化 / 一日游 |
| `yesterday_limit_up_premium` | 昨日涨停股次日表现 | 昨日涨停整体溢价 | 一日游 / 退潮 |
| `breadth` | 板块成分股涨跌分布 | 板块内部上涨家数占比 | 分化 / 退潮 |
| `net_inflow_ratio` | 资金流 / 成交额 | 净流入占成交额比 | 启动 / 加速 / 高潮 |
| `vol_ratio` | K 线 | 板块量比 | 启动 / 加速 / 退潮确认 |

### 3.3 当前系统已实际接入的字段

当前系统已经在不同链路中接入了以下维度：

- 涨停家数
- 连板家数
- 最高板
- 资金净流入
- 近 3 日资金流
- 前一日状态
- 活跃天数
- 近 5 日活跃度
- K 线趋势
- 收盘价相对 MA20

当前系统尚未稳定纳入统一判定的关键维度：

- 跌停家数
- 封板率
- 炸板率
- 晋级率
- 昨日涨停溢价
- 板块内部涨跌分布

---

## 4. 判定优先级

建议统一按以下优先级判定，避免状态冲突：

1. `climax` 高潮
2. `declining` 退潮
3. `diverging` 分化
4. `accelerating` 加速中
5. `emerging` 刚启动
6. `one_day` 一日游
7. `dormant` 休眠

说明：

- 高潮是最强正向状态，需优先识别
- 退潮是最强负向状态，需高于分化
- 分化本质是从高潮/加速向下的过渡状态
- 一日游必须依赖前序状态，不可孤立判定
- 休眠必须作为最终兜底状态，不能被前置条件覆盖

---

## 5. 各状态判定标准

以下为建议统一口径。

### 5.1 刚启动 `emerging`

| 维度 | 建议阈值 |
| --- | --- |
| 涨停家数 | `2 <= limit_up_count <= 4`，或 `limit_up_count == 1` 且资金/趋势明显转强 |
| 连板家数 | `consecutive_board_count <= 2` |
| 最高板 | `max_board_height <= 2` |
| 资金流 | `fund_flow > 1` |
| 趋势 | `kline_close >= kline_ma5` 或 `kline_trend in ("up", "breakout_up")` |
| 前序状态约束 | 前一日不能是 `climax / accelerating` |

业务描述：

- 有启动信号，但梯队未完全形成
- 可以关注，不宜直接视作主升段

### 5.2 加速中 `accelerating`

| 维度 | 建议阈值 |
| --- | --- |
| 涨停家数 | `limit_up_count >= 5` |
| 连板家数 | `consecutive_board_count >= 3` |
| 最高板 | `max_board_height >= 3` |
| 活跃天数 | `active_days >= 2` |
| 资金流 | `fund_flow > 3` 或 `fund_flow_3d > 8` |
| 趋势 | `kline_close >= kline_ma5 >= kline_ma20` 或趋势向上 |

业务描述：

- 板块进入主升结构
- 连板梯队、资金、趋势三者开始共振

### 5.3 高潮 `climax`

| 维度 | 建议阈值 |
| --- | --- |
| 涨停家数 | `limit_up_count >= 10` |
| 连板家数 | `consecutive_board_count >= 5` |
| 最高板 | `max_board_height >= 5` |
| 资金流 | `fund_flow > 5` |
| 趋势 | 趋势仍强，但需观察是否加速过陡 |
| 反馈增强项 | `seal_rate >= 75%` 更可靠 |

业务描述：

- 板块处于最亢奋阶段
- 高风险高波动，不应等同于“最优追涨点”

### 5.4 分化 `diverging`

| 维度 | 建议阈值 |
| --- | --- |
| 前序状态 | 前一日必须是 `accelerating` 或 `climax` |
| 涨停衰减 | 相比前一状态明显减少，或降至 `limit_up_count < 5` |
| 梯队走弱 | `max_board_height < 4` 或 `consecutive_board_count` 明显下降 |
| 反馈走弱 | 炸板率上升、晋级率下降、后排掉队增多 |
| 趋势 | 指数高位震荡或不再同步走强 |

业务描述：

- 龙头可能还强，但板块内部已经不同步
- 是从强转弱的过渡带

### 5.5 退潮 `declining`

| 维度 | 建议阈值 |
| --- | --- |
| 前序状态 | 前一日为 `climax / accelerating / diverging` |
| 梯队瓦解 | `consecutive_board_count == 0` 或 `max_board_height <= 1` |
| 跌停/回撤 | `limit_down_count >= 2` 或明显回撤股增多 |
| 资金流 | `fund_flow < -5` 或 `fund_flow_3d < -15` |
| 技术面 | `kline_trend in ("down", "breakdown")` 或 `kline_close < kline_ma20 * 0.97` |

业务描述：

- 不是简单变弱，而是结构被破坏
- 退潮必须依赖前序活跃状态，不应把普通弱势板块误打成退潮

### 5.6 一日游 `one_day`

| 维度 | 建议阈值 |
| --- | --- |
| 前序状态 | 前一日是 `emerging` |
| 次日持续性 | 今日 `limit_up_count <= 1` |
| 活跃延续 | `active_days == 1` 或未形成持续活跃 |
| 反馈 | 昨日涨停无溢价、无晋级、无扩散 |
| 资金流 | `fund_flow <= 0` 更可信 |

业务描述：

- 必须是“昨天启动，今天熄火”
- 不是所有弱板块都叫一日游

### 5.7 休眠 `dormant`

| 维度 | 建议阈值 |
| --- | --- |
| 涨停家数 | `limit_up_count == 0` |
| 连板家数 | `consecutive_board_count == 0` |
| 资金流 | `fund_flow <= 0` |
| 活跃天数 | `active_days == 0` |
| 趋势 | 无突破，无持续放量 |

业务描述：

- 没有交易价值信号
- 应作为最终兜底状态

---

## 6. 状态流转规则

### 6.1 正常正向流转

`dormant -> emerging -> accelerating -> climax`

### 6.2 强转弱流转

`climax -> diverging -> declining`

或

`accelerating -> diverging -> declining`

### 6.3 失败启动流转

`emerging -> one_day -> dormant`

### 6.4 退潮后重启

`declining -> dormant -> emerging`

说明：

- 不建议直接从 `declining -> accelerating`
- 不建议直接从 `dormant -> climax`
- `one_day` 应该只作为 `emerging` 的失败分支，不应覆盖其他状态

---

## 7. 当前系统现状评估

### 7.1 已有能力

- 已有生命周期状态机引擎
- 已有涨停家数 / 连板 / 龙头高度 / 资金流 / 前一日状态
- 已有日 K 技术因子读取能力
- 已有生命周期表和历史状态表

### 7.2 当前主要问题

1. 存在两套判定逻辑
   - 一套在 `backend/app/sector/lifecycle.py`
   - 一套在 `backend/app/api/v1/sectors.py`
   - 两套逻辑字段和优先级不一致

2. `休眠` 与 `一日游` 的边界容易被错误覆盖
   - 若兜底条件顺序不当，`dormant` 会不可达

3. `consecutive_days / active_days` 计算可能失真
   - 会直接影响 `刚启动 / 加速中 / 一日游`

4. 生命周期计算覆盖不完整
   - 当前只对部分板块计算权威状态，其余板块可能走简化路径

5. 缺少反馈类字段
   - 没有统一纳入炸板率、封板率、晋级率、昨日涨停溢价
   - 导致 `分化 / 退潮 / 一日游` 精度不足

---

## 8. 建议的统一实现原则

### 8.1 唯一判定源

生命周期状态只允许通过一套引擎产生：

- 统一使用 `SectorLifecycleEngine`
- API 层不得再自行维护第二套判定规则

### 8.2 盘中与盘后统一口径

- 盘中：
  - 用实时资金流 + 实时涨停池 + 最新板块快照
  - 结合前一交易日 K 线与历史状态
- 盘后：
  - 用收盘口径重算并落库

### 8.3 K 线用途

K 线不建议主导 `启动 / 加速 / 高潮`，但必须参与：

- `分化` 辅助确认
- `退潮` 强确认
- 趋势性过滤（假启动）

### 8.4 状态判定结构

建议统一为：

1. 先判最强正向：`climax`
2. 再判最强负向：`declining`
3. 再判中间过渡：`diverging`
4. 再判正向推进：`accelerating`
5. 再判早期启动：`emerging`
6. 再判失败启动：`one_day`
7. 最后兜底：`dormant`

---

## 9. 建议测试样例

至少为每个状态准备 3 类测试：

### 9.1 单状态命中测试

- 输入一组字段，确保只命中目标状态

### 9.2 边界测试

- 如 `limit_up_count = 4/5`
- `max_board_height = 2/3`
- `fund_flow = 3/5`

### 9.3 流转测试

- `emerging -> one_day`
- `accelerating -> diverging`
- `climax -> declining`
- `declining -> dormant -> emerging`

---

## 10. 最终建议

如果按实盘可用性排序，生命周期判定最重要的维度依次是：

1. 涨停家数
2. 连板梯队
3. 最高板高度
4. 当日与近 3 日资金流
5. 前一日状态
6. 板块指数相对 MA5 / MA20 的位置
7. 炸板率 / 晋级率 / 昨日涨停溢价

一句话总结：

**板块生命周期 = 涨停结构 + 资金强弱 + 历史状态 + 趋势确认 + 次日反馈。**


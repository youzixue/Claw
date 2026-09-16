# 个股日K线 + 实时行情 — 字段设计最佳实践

> 日期: 2026-04-14 | 设计原则: 因子驱动 × 数据源验证 × 最小冗余

---

## 一、设计原则

1. **因子驱动**: 每个字段必须有明确的消费方（因子/信号/前端），无消费方不存
2. **数据源验证**: 只存数据源真实返回的字段，可计算的不重复存
3. **最小冗余**: 不存可从已有字段计算出的派生字段，除非计算成本极高
4. **板块K线参考**: SectorKline已跑通ma5/ma10/ma20/ma5_vol/trend_state/vol_ratio/support/resistance，个股照搬

---

## 二、因子依赖字段汇总（48因子逐项提取）

### 2.1 直接从stock_kline读取的字段

| 字段 | 因子 | 使用方式 | 必须度 |
|------|------|---------|--------|
| **close** | ma5_bias, ma20_bias, macd_signal, rsi_14, kdj_golden, boll_position, breakout_energy, acceleration_signal, chip_structure, topping_warning | 5-30日窗口rolling计算 | 🔴必须 |
| **high** | kdj_golden, topping_warning | 9日rolling min/max | 🔴必须 |
| **low** | kdj_golden, topping_warning | 9日rolling min/max | 🔴必须 |
| **volume** | volume_ratio, volume_spike, breakout_energy, acceleration_signal, topping_warning | 5-21日rolling mean | 🔴必须 |
| **turnover** | turnover_rate, divergence_degree | 当日值 | 🔴必须 |
| **amplitude** | divergence_degree | 当日值 | 🟡重要 |
| **prev_close** | (计算涨跌幅用) | 当日值 | 🔴必须 |
| **amount** | fund_concentration, big_order_pct, retail_outflow_pct | 当日值+FundFlow联动 | 🟡重要 |
| **change_pct** | (前端展示+信号检测) | 当日值 | 🔴必须 |

### 2.2 从stock_kline计算的字段（不存，计算得出）

| 派生字段 | 计算方式 | 因子 |
|---------|---------|------|
| ma5/ma10/ma20/ma60 | `close.rolling(N).mean()` | ma5_bias, ma20_bias, boll_position, breakthrough |
| boll_upper/boll_lower | `ma20 ± 2*std20` | boll_position |
| vol_ratio(量比) | `volume / volume.rolling(5).mean().shift(1)` | volume_ratio |
| rsi_14 | delta/gain/loss rolling | rsi_14 |
| dif/dea/macd_hist | ema12/ema26 | macd_signal |
| k/d/j | rsv.ewm | kdj_golden |
| support/resistance | `low.rolling(20).min()` / `high.rolling(20).max()` | breakthrough |
| high_20d/60d/120d | `high.rolling(N).max().shift(1)` | breakthrough |
| recent_closes | `close.iloc[-30:]` | breakthrough, chip |

### 2.3 从stock_spot(实时行情)读取的字段

| 字段 | 信号模块 | 用途 | 必须度 |
|------|---------|------|--------|
| **price(最新价)** | breakthrough | 突破检测 | 🔴 |
| **volume** | capital_anomaly | 量比计算 | 🔴 |
| **change_pct** | capital_anomaly | 涨跌幅异动 | 🔴 |
| **open/high/low/close** | capital_anomaly(冲高回落) | 诱多检测 | 🔴 |
| **volume_ratio(量比)** | capital_anomaly, breakthrough | 异动+突破质量 | 🔴 |
| **main_net_inflow** | capital_anomaly, dragon_head | 资金异动 | 🔴 |
| **turnover** | dragon_head, limit_up_tracker | 龙头辨识+封板质量 | 🔴 |
| **limit_up_price** | (风控) | 涨停价判定 | 🔴 |
| **limit_down_price** | (风控) | 跌停价判定 | 🟡 |
| **avg_price(VWAP)** | (交易) | 均价 | 🟡 |
| **bid_ratio(委比)** | (信号) | 盘口强弱 | 🟡 |
| **5min_change** | (短线) | 5分钟异动 | 🟡 |

### 2.4 不需要存入stock_kline/stock_spot的字段

| 字段 | 来源 | 原因 |
|------|------|------|
| big_net_inflow / small_net_inflow | FundFlow表 | 已有独立表，不重复 |
| margin_buy / margin_balance | MarginData表 | 已有独立表 |
| sector_strength / sector_persistence | SectorStrength/SectorPersistence表 | 已有独立表 |
| sentiment_cycle | MarketSentiment表 | 已有独立表 |
| news_count / news_sentiment | FinanceNews表 | 已有独立表 |
| ma5/ma10/ma20/ma60 | 可从close计算 | 不存计算字段 |
| boll_upper/boll_lower | 可从close计算 | 不存计算字段 |
| trend_state | 可从close+ma计算 | 不存计算字段 |

---

## 三、stock_kline 表设计（最终版）

### 3.1 核心字段（同花顺接口直接返回）

```sql
CREATE TABLE stock_kline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        VARCHAR(10) NOT NULL,          -- 股票代码 '000001'
    trade_date  DATE NOT NULL,                 -- 交易日期
    period      VARCHAR(5) NOT NULL DEFAULT 'daily', -- 周期: daily/weekly/monthly
    
    -- OHLCV（同花顺原始字段）
    open        FLOAT,                         -- 开盘价
    high        FLOAT,                         -- 最高价
    low         FLOAT,                         -- 最低价
    close       FLOAT,                         -- 收盘价
    volume      BIGINT,                        -- 成交量(股) ⚠️ 注意:同花顺返回股,非手
    amount      FLOAT,                         -- 成交额(元)
    turnover    FLOAT,                         -- 换手率(%)
    
    -- 复权类型标记
    adjust      VARCHAR(3) NOT NULL DEFAULT 'qfq', -- 复权类型: qfq/hfq/none
    
    -- 数据源
    source      VARCHAR(20) DEFAULT 'ths',     -- ths/akshare/sina/tencent
    
    -- 唯一约束 + 索引
    UNIQUE(code, trade_date, period, adjust),
    INDEX ix_kline_date (trade_date),
    INDEX ix_kline_code_date (code, trade_date)
);
```

### 3.2 字段决策详解

| 字段 | 是否存 | 理由 |
|------|--------|------|
| open/high/low/close | ✅ 存 | OHLC是因子计算根基 |
| volume | ✅ 存 | 成交量是量比/异动因子输入 |
| amount | ✅ 存 | 成交额=计算VWAP/资金集中度的必要输入，同花顺直接返回 |
| turnover | ✅ 存 | 换手率=因子directInput，无法从OHLCV计算(需流通股本) |
| **change_pct** | ✅ 存 | 虽可从prev_close算，但使用频率极高(48因子中6个直接用)，存了避免每次join |
| **prev_close** | ✅ 存 | 计算涨跌幅/振幅的必要字段，同花顺不返回但可从前日close取 |
| **amplitude** | ❌ 不存 | 可从(high-low)/prev_close计算，1行代码 |
| ma5/ma10/ma20/ma60 | ❌ 不存 | 从close列rolling计算，因子引擎自带 |
| trend_state | ❌ 不存 | 从close+ma计算，非原始数据 |
| vol_ratio | ❌ 不存 | 从volume列rolling计算 |
| support/resistance | ❌ 不存 | 从high/low列rolling计算 |

### 3.3 为什么不存计算字段？

**板块K线存了ma5/trend_state等，为什么个股不存？**

| 维度 | 板块K线 | 个股K线 |
|------|---------|---------|
| 记录数 | 649板块×250天≈16万 | 5000股×1758天≈**879万** |
| 存ma5等 | 多6列×16万≈96万值 | 多6列×879万≈5274万值 |
| 计算成本 | 低(板块少) | 低(rolling极快) |
| 查询频率 | 低(板块营地) | 高(因子引擎每次全量读) |

个股K线是**879万行**的大表，每多一列就多879万个浮点数。计算字段不存，因子引擎读取后用pandas rolling一行代码算出。板块K线只有16万行，存计算字段是为了前端展示直接用。

### 3.4 与现有StockDaily的关系

**废弃StockDaily，用stock_kline替代**。原因：

| 维度 | StockDaily | stock_kline |
|------|-----------|-------------|
| 数据量 | 只有3条(指数) | 全量5000+只 |
| period | 无 | daily/weekly/monthly |
| adjust | 无 | qfq/hfq/none |
| 数据源 | 无标记 | ths/akshare/sina |
| 字段 | 同 | 同(加上change_pct/prev_close) |

迁移方案：新建stock_kline，保留StockDaily只存指数(000001/399001/399006)。

---

## 四、stock_spot 表设计（最终版）

### 4.1 字段设计

```sql
CREATE TABLE stock_spot (
    code        VARCHAR(10) PRIMARY KEY,       -- 股票代码
    updated_at  DATETIME NOT NULL,             -- 最后更新时间
    
    -- === 价格(6字段) ===
    price       FLOAT,                         -- [3]最新价
    prev_close  FLOAT,                         -- [4]昨收
    open        FLOAT,                         -- [5]今开
    high        FLOAT,                         -- [33]最高
    low         FLOAT,                         -- [34]最低
    avg_price   FLOAT,                         -- [51]均价(VWAP)
    
    -- === 涨跌(4字段) ===
    change      FLOAT,                         -- [31]涨跌额
    change_pct  FLOAT,                         -- [32]涨跌幅%
    amplitude   FLOAT,                         -- [43]振幅%
    min5_change FLOAT,                         -- [62]5分钟涨跌%
    
    -- === 量能(4字段) ===
    volume      BIGINT,                        -- [6]成交量(手)
    amount      FLOAT,                         -- [37]成交额(万)
    volume_ratio FLOAT,                        -- [49]量比
    turnover    FLOAT,                         -- [38]换手率%
    
    -- === 资金(3字段) ===
    main_net_inflow FLOAT,                     -- [50]主力净流入(手)
    outer_vol   INT,                           -- [7]外盘(手)=主买
    inner_vol   INT,                           -- [8]内盘(手)=主卖
    
    -- === 估值(4字段) ===
    pe_ttm      FLOAT,                         -- [39]TTM市盈率
    pb          FLOAT,                         -- [46]市净率
    circ_market_cap FLOAT,                     -- [44]流通市值(亿)
    total_market_cap FLOAT,                    -- [45]总市值(亿)
    
    -- === 限价(2字段) ===
    limit_up    FLOAT,                         -- [47]涨停价
    limit_down  FLOAT,                         -- [48]跌停价
    
    -- === 盘口(2字段) ===
    bid_ratio   FLOAT,                         -- [56]委比%
    bid_vol     BIGINT,                        -- 买一~买五总量(手)
    
    -- === 区间(2字段) ===
    week52_high FLOAT,                         -- [67]52周最高
    week52_low  FLOAT,                         -- [68]52周最低
    
    -- === 股本(2字段) ===
    total_shares  BIGINT,                      -- [72]总股本(股)
    circ_shares   BIGINT,                      -- [73]流通股本(股)
    
    -- === 数据源 ===
    source      VARCHAR(20) DEFAULT 'tencent'  -- tencent/sina
);
```

### 4.2 字段决策详解

| 腾讯字段 | 是否存 | 理由 |
|---------|--------|------|
| [1]名称 | ❌ | StockTag.name已有 |
| [2]代码 | ✅ PK | |
| [3]最新价 | ✅ | 突破检测+前端展示 |
| [4]昨收 | ✅ | 涨跌幅计算基准 |
| [5]今开 | ✅ | 冲高回落检测(open_price参数) |
| [6]成交量 | ✅ | 量比计算 |
| [7]外盘 | ✅ | 主买量，内外盘比=多空力量 |
| [8]内盘 | ✅ | 主卖量 |
| [9-28]买五卖五 | ❌ | 存总量bid_vol/ask_vol即可，明细前端按需取 |
| [30]时间 | ❌ | updated_at替代 |
| [31]涨跌额 | ✅ | 前端展示 |
| [32]涨跌幅% | ✅ | 核心字段，异动检测+前端 |
| [33]最高 | ✅ | 冲高回落+振幅 |
| [34]最低 | ✅ | 冲高回落+振幅 |
| [37]成交额(万) | ✅ | 资金集中度因子 |
| [38]换手率% | ✅ | 核心因子输入 |
| [39]TTM市盈率 | ✅ | 估值因子 |
| [43]振幅% | ✅ | 分歧度因子 |
| [44]流通市值(亿) | ✅ | 封板强度=封单/流通市值 |
| [45]总市值(亿) | ✅ | 前端展示+选股过滤 |
| [46]市净率 | ✅ | 估值因子 |
| [47]涨停价 | ✅ | 风控+涨停判定 |
| [48]跌停价 | ✅ | 风控+跌停判定 |
| [49]量比 | ✅ | 核心因子，异动检测 |
| [50]主力净流入(手) | ✅ | 资金异动+龙头辨识 |
| [51]均价VWAP | ✅ | 机构成本参考 |
| [52]动态PE | ❌ | 与[39]重复 |
| [53]静态PE | ❌ | 与[39]重复 |
| [56]委比% | ✅ | 盘口强弱信号 |
| [62]5分钟涨跌% | ✅ | 短线异动信号 |
| [67-68]52周高低 | ✅ | 突破检测(突破52周新高) |
| [72-73]总/流通股本 | ✅ | 换手率计算验证+选股过滤 |
| [85]昨收(?) | ❌ | 与[4]可能重复，不确定含义不存 |
| [86]主力净流入(?) | ❌ | 与[50]可能重复，不确定不存 |

### 4.3 为什么是单行覆盖(UPSERT)而非追加？

| 维度 | 方案A: 追加(每轮一条) | 方案B: 覆盖(只存最新) |
|------|---------------------|---------------------|
| 存储 | 5000只×4小时×120轮=240万条/天 | **5000只×1条=5000条** |
| 查询 | 需WHERE时间最新 | 直接SELECT |
| 历史 | 可回溯 | 不可回溯(靠stock_kline) |
| 速度 | INSERT慢 | UPSERT快 |

选方案B。盘中行情是**快照**，历史行情靠stock_kline。覆盖写=4秒一轮全量更新，极简。

---

## 五、消费方映射表

### 5.1 stock_kline消费方

| 消费方 | 需要字段 | 读取方式 |
|--------|---------|---------|
| 因子引擎-技术因子(8) | close, high, low, volume, turnover | `SELECT ... WHERE code=? ORDER BY trade_date DESC LIMIT 30` |
| 因子引擎-爆发因子(5) | close, volume, amount | 同上 |
| 因子引擎-生命周期因子(5) | close, high, low, volume, amplitude, turnover | 同上 |
| 因子引擎-晋级因子(1) | close | 同上 |
| 突破信号检测 | close(近30日) | `SELECT close ... LIMIT 30` |
| 资金异动-冲高回落 | (用stock_spot实时) | - |
| 前端-个股详情K线图 | OHLCV + amount + turnover + change_pct | `SELECT ... LIMIT 250` |
| 回测引擎 | OHLCV全量 | 大范围查询 |

### 5.2 stock_spot消费方

| 消费方 | 需要字段 |
|--------|---------|
| 资金异动检测 | price, volume, change_pct, open/high/low, volume_ratio, main_net_inflow |
| 龙头股扫描 | change_pct, main_net_inflow, turnover |
| 突破信号检测 | price, volume, high_52w |
| 前端-行情总览 | 全字段 |
| 前端-牛股雷达 | change_pct, volume_ratio, main_net_inflow, turnover |
| 风控-涨停价判定 | limit_up, limit_down, price |
| 前端-个股详情实时 | price, change_pct, volume, amount, turnover, pe, pb, market_cap |

---

## 六、采集策略

### 6.1 stock_kline（同花顺为主）

| 模式 | 时机 | 范围 | 速度 |
|------|------|------|------|
| init | 首次/全量 | 5000只×1758条(前复权) | 0.06s/只×50并发≈6分钟 |
| daily | 盘后15:30 | 5000只×1条 | ≈6秒 |
| repair | 每月1次 | 5000只×30条(修正漂移) | ≈3秒 |

采集字段映射（同花顺→DB）:
```
[0]日期     → trade_date
[1]开盘     → open
[2]最高     → high  
[3]最低     → low
[4]收盘     → close
[5]成交量   → volume (股)
[6]成交额   → amount (元)
[7]换手率   → turnover (%)
```

prev_close: 从前日记录取 or DB查SELECT close WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1
change_pct: (close - prev_close) / prev_close * 100

### 6.2 stock_spot（腾讯为主）

| 时机 | 频率 | 范围 | 速度 |
|------|------|------|------|
| 盘中 | 30秒/轮 | 5000只全量 | 50批×0.08s≈4秒 |
| 盘后 | 停止 | - | - |

采集字段映射（腾讯→DB）:
```
[2]  → code
[3]  → price
[4]  → prev_close
[5]  → open
[32] → change_pct
[33] → high
[34] → low
[6]  → volume (手→股: ×100)
[37] → amount (万→元: ×10000)
[38] → turnover
[39] → pe_ttm
[43] → amplitude
[44] → circ_market_cap
[45] → total_market_cap
[46] → pb
[47] → limit_up
[48] → limit_down
[49] → volume_ratio
[50] → main_net_inflow (手→股: ×100)
[51] → avg_price
[56] → bid_ratio
[62] → min5_change
[67] → week52_high
[68] → week52_low
[72] → total_shares
[73] → circ_shares
```

---

## 七、与现有系统的集成点

1. **因子引擎**: 直接从stock_kline读取DataFrame，pandas rolling计算均线/指标
2. **信号模块**: 从stock_spot读实时行情做异动/突破检测
3. **前端-个股详情**: stock_kline(K线图) + stock_spot(实时行情面板)
4. **前端-行情总览**: stock_spot全量排行(按涨跌幅/量比/资金流入排序)
5. **调度器**: 新增2个定时任务(stock_kline_daily + stock_spot_realtime)
6. **StockDaily**: 保留只存3大指数，个股K线迁移到stock_kline

# 强势排行Tab评审报告 (2026-04-15)

## 一、数据新鲜度

### 结论: ❌ 非最新交易数据，每次打开都重新计算

**当前机制**:
- 强势排行Tab使用 `lazy` 属性，首次切换时才加载
- 每次点击Tab都调用 `loadRank()` → `GET /api/v1/tenbagger/rank`
- API无缓存，每次请求都执行完整两轮评分计算
- 切换模式(bull↔tenbagger)也会重新请求

**问题**:
- 10.4秒响应时间严重影响用户体验
- 无服务端缓存，重复计算浪费资源
- 盘中数据变化时不会自动刷新

---

## 二、字段逻辑评审

### 2.1 BullScoreModel (短线强势) - ✅ 基本正确

| 维度 | 权重 | 逻辑 | 评审 |
|------|------|------|------|
| 动量强度 | 20% | 涨幅分档+连板加成+5日动量+量比 | ✅ 合理 |
| 资金面 | 25% | 当日净流入+5日累计 | ✅ 合理 |
| 技术形态 | 20% | MA趋势+BOLL+突破新高 | ✅ 合理 |
| 估值安全 | 10% | PE/PB分档 | ⚠️ 需优化 |
| 基本面 | 10% | 净利润增速 | ✅ 合理 |
| 规模适配 | 5% | 流通市值分档 | ✅ 合理 |
| 活跃度 | 10% | 换手+量比+振幅 | ✅ 合理 |

**问题发现**:

1. **circ_market_cap单位混乱** (已修复)
   - DB存的是**亿**单位
   - bull_score.py 第293行: `cap_billion = circ_market_cap or 0` ✅正确
   - 但注释写的是"元"，容易误导

2. **涨停判断逻辑** (tenbagger.py 695-696行)
   ```python
   is_limit_up = (spot.limit_up and spot.price and spot.price >= spot.limit_up)
   is_one_word = is_limit_up and spot.open and spot.open >= spot.limit_up
   ```
   - ✅ 正确: 现价≥涨停价 = 涨停
   - ✅ 正确: 开盘价≥涨停价 = 一字板

3. **5日涨幅计算** (tenbagger.py 686-688行)
   ```python
   change_5d_map[code] = round((closes[-1] / closes[0] - 1) * 100, 2)
   ```
   - ⚠️ **逻辑错误**: `closes[0]`是最早的，`closes[-1]`是最新的
   - 应该是 `(最新/最早 - 1)`，但注释说是"5日涨幅"
   - 实际上代码是对的: reversed后从远到近，[-1]是最新

4. **量比阈值** (bull_score.py 142-145行)
   ```python
   if volume_ratio > 3:  # +5分
   elif volume_ratio > 2:  # +3分
   ```
   - ✅ 符合A股惯例: 量比>2为放量，>3为明显放量

5. **缩量涨停降级** (bull_score.py 147-150行)
   ```python
   if is_limit_up and volume_ratio < 0.8:
       momentum_score -= 15
       risk_warnings.append("缩量涨停⚠️")
   ```
   - ✅ 正确: 缩量涨停可能封不住，需要警示

### 2.2 TenbaggerModel (十倍潜力) - ✅ 逻辑正确

| 维度 | 权重 | 逻辑 | 评审 |
|------|------|------|------|
| 市值起点 | 25% | <50亿=95分, <100亿=80分 | ✅ 小市值弹性大 |
| 增速动力 | 25% | 净利润增速分档 | ✅ >100%得95分合理 |
| 估值合理性 | 20% | PE/PB综合 | ⚠️ 阈值偏宽 |
| 赛道爆发力 | 15% | 板块持续天数 | ✅ 3天=70分合理 |
| 资金认可度 | 15% | 5日净流入为主 | ✅ 正确 |

**问题发现**:

1. **PE阈值过宽** (tenbagger_model.py 115-127行)
   ```python
   if pe_ttm < 20: +25分
   elif pe_ttm < 40: +15分
   elif pe_ttm < 80: +0分  # 这里太宽了
   elif pe_ttm < 150: -15分
   ```
   - ⚠️ PE 40-80不给分也不扣分，对成长股太宽松
   - 建议: PE>60就开始扣分

2. **sector_days来源** (tenbagger.py 894-908行)
   ```python
   for sector_code in code_sectors[code]:
       sp = sector_persistence_map.get(sector_code)
       if sp:
           if (sp.consecutive_days or 0) > best_days:
               best_days = sp.consecutive_days or 0
   ```
   - ✅ 正确: 取所属板块中持续性最好的
   - 但一个股票可能属于多个板块，取最大值合理

### 2.3 前端字段映射 - ✅ 正确

| 字段 | 来源 | 显示 | 状态 |
|------|------|------|------|
| change_pct | StockSpot | 涨跌幅 | ✅ |
| turnover | StockSpot | 换手率% | ✅ |
| volume_ratio | StockSpot | 量比 | ✅ |
| main_net_inflow_billion | 计算/1e8 | 主力(亿) | ✅ |
| circ_market_cap_billion | StockSpot(亿) | 市值(亿) | ✅ |
| pe_ttm | StockSpot | PE | ✅ |
| net_profit_growth | StockSpot | 增速% | ✅ |
| pb | StockSpot | PB | ✅ |

---

## 三、性能瓶颈分析

### 3.1 当前耗时分布 (10.4秒)

```
_bull_rank() 耗时分解:
├── 1. 批量读取spot (~5100只)     ~0.3s
├── 2. 批量读取涨停池              ~0.1s
├── 3. 批量读取5日资金流           ~0.2s
├── 4. 批量读取近5日K线            ~0.5s
├── 5. 第1轮评分(5100只)           ~0.1s
├── 6. 第2轮K线查询(200只×120条)   ~8.0s ⚠️ 瓶颈
├── 7. 第2轮评分(200只)            ~0.1s
└── 8. filter_signals              ~0.5s
```

### 3.2 瓶颈定位

**第6步: 逐只查询K线** (tenbagger.py 746-756行)
```python
for code in top200_codes:  # 200次循环
    kline_result = await db.execute(
        text("""
            SELECT close, high FROM stock_kline
            WHERE code = :code
            ORDER BY trade_date DESC
            LIMIT 120
        """),
        {"code": code}
    )
```

- ❌ 200次单独查询，每次RTT ~40ms
- ❌ SQLite异步IO开销大
- ❌ 无并行查询

### 3.3 优化建议

**方案1: 服务端缓存 (推荐)**
```python
# 添加缓存装饰器
@cache_with_ttl(ttl=60)  # 1分钟缓存
async def _bull_rank(db, limit):
    ...
```

**方案2: 批量K线查询**
```python
# 一次性查询200只的K线
SELECT code, close, high FROM stock_kline
WHERE code IN (..., ...) 
AND trade_date >= (SELECT MAX(trade_date) FROM stock_kline) - 120
```

**方案3: 预计算技术维度**
- 在scheduler中定时计算MA/BOLL存入StockSpot
- API直接读取，无需实时计算

**方案4: 前端缓存+增量更新**
- 首次加载后缓存结果
- 定时轮询只拉取变化部分

---

## 四、改进建议汇总

### P0 - 立即修复

1. **添加服务端缓存** (预计优化到<2秒)
   ```python
   # tenbagger.py
   _RANK_CACHE = {}
   _RANK_CACHE_TTL = 60  # 秒
   ```

2. **修复PE阈值** (tenbagger_model.py)
   ```python
   elif pe_ttm < 60: val_score += 0   # 从80改为60
   elif pe_ttm < 100: val_score -= 15 # 从150改为100
   ```

### P1 - 短期优化

3. **批量K线查询** 替代逐只查询
4. **添加缓存预热** 在scheduler中定时刷新
5. **前端添加loading状态** 避免用户重复点击

### P2 - 长期改进

6. **预计算技术维度** 存入StockSpot表
7. **WebSocket推送** 排名变化实时更新
8. **增量更新机制** 只更新变化的股票

---

## 五、结论

| 项目 | 状态 | 说明 |
|------|------|------|
| 数据新鲜度 | ❌ | 非最新，每次重新计算 |
| 字段逻辑 | ✅ | 7维/5维评分逻辑正确 |
| 单位换算 | ✅ | circ_market_cap=亿正确 |
| 涨停判断 | ✅ | 现价/开盘价判断正确 |
| 性能 | ❌ | 10.4秒 unacceptable |
| 缓存 | ❌ | 无服务端缓存 |

**总体评价**: 逻辑正确但性能需优化，建议立即添加服务端缓存。

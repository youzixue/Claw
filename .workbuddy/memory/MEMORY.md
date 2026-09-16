# Claw 量化交易系统 - 工作记忆

## 项目概览
- **项目代号**: Claw（鹰爪）
- **类型**: A股多因子量化交易系统
- **技术栈**: Python FastAPI + Vue3 Vite6 + Element Plus + ECharts(深色) + SQLite/PostgreSQL
- **部署**: Docker Compose
- **交易限制**: 主板✅可交易, 创业板/科创板/北交所👁️仅观察, ST/停牌/退市🚫不推

## 数据源(七源互补)
- **同花顺(AkShare)**: 板块+资金流(5190只)+技术选股+财务
- **pywencai**: 个股→行业257+概念388映射 + 涨停/连板 + loop=True必传
- **东财**: 涨停/跌停/炸板(10s) + 龙虎榜 + 大盘资金流
- **申万**: 413个行业(三级)+权重+PE/PB/股息率 + 5805只映射
- **新浪**: 175概念+49行业 成分股明细(含个股行情)
- **腾讯(qt.gtimg.cn)**: 实时行情88字段→23字段stock_spot表 ✅已实现
- **同花顺K线(d.10jqka.com.cn)**: 日K全量1758条+前复权→stock_kline表 ✅已实现

## 腾讯实时字段(2026-04-14 APP验证)
- **已确认**: [3]最新价 [4]昨收 [5]开 [6]量 [32]涨幅% [33]高 [34]低 [38]换手% [39]PE_TTM [44]流通市值 [45]总市值(高估值+8.5%) [46]PB(高估值+13%) [47]涨停 [48]跌停 [49]量比✅ [50]五档委差(手，非资金) [51]VWAP✅ [64]股息率TTM✅ [72]总股本 [73]流通股本 [74]委比 [79]净利润增速%✅
- **[50]重要纠正(2026-09-09原始88字段复核)**: 6/6实时接口样本、9/8库存5218/5218行等于五档买量减卖量，单位手；不是主力资金，不能乘价变成主力净额。新腾讯采集main_net_inflow=NULL，实时资金从FundFlow真实主力字段及源/接收/观测时钟验收；旧行不回填。证据见outputs/anomaly-fund-source-audit-20260909.md。映射修复不等于真实资金源恢复。
- **腾讯主力资金独立接入(2026-09-09)**: hsfundtab组合todayFundFlow,todayFundTrend→FundFlow，source=tencent/version=tencent_hsfundtab_v1；不是field50。30s调度/最多6并发/每轮600只滚动/20s协程预算；净占比按四类双边资金总额一半作为成交额分母，源分钟与汇总严格绑定。已在原8000部署src-txfund-1d9cd5a217181d267bb7/PID55077，全套3609passed61skipped，14:36合格5158/5225。旧个股资金在线主备路由删除，其他东财服务及历史保留。仍有SQLite争用导致约79s轮次及健康超时，不声称全服务稳定；详见outputs/tencent-fund-replacement-20260909.md。
- **[62]重要纠正(2026-08-04原始88字段复核)**: 当前A股返回的是长周期涨跌值，不是5分钟涨跌；3128只中2246只绝对值>10%，不得作为分钟动能。采集端置0，异动扫描改用连续spot快照计算真实5分钟涨跌。
- **9个线上错误映射已纠正**: [41]=最高价(非流通市值) [42]=最低价(非总市值) [43]=振幅(非PB) [44]=流通市值(非涨停价) [45]=总市值(非跌停价) [47]=涨停价(非均价) [48]=跌停价(非委比) [51]=VWAP(非主力占比) [74]=委比%(原未知)
- **高估值偏差**: 茅台[45]+8.5%/[46]+13%, 高价股用[44]或自算更准
- **THS K线**: [9][10]=盘后定价交易量/额(仅创业板/科创板), 用户不交易故不存

## 牛股雷达最佳实践方案v2.0(2026-04-14 评审修订→已实现)
- **新增2表**: stock_spot(26字段含元数据,单行覆盖) + stock_kline(13字段,前复权,唯一约束code+date)
- **新增2采集任务**: 腾讯实时30s/轮 + 同花顺日K盘后15:10一次 ✅scheduler已注册
- **新增8个文件**: tencent_source/ths_kline_source/anomaly_scanner(~350行)/tenbagger_model/spot API/tenbagger API + 前端2个vue
- **修改4个文件**: scheduler(main+2tasks+2sources)/main.py(+spot路由)/leader_tracker(+StockSpot市值)/api/index.js(+7API)
- **牛股雷达5Tab**: 异动监控(温度计+异动流)/强势排行(短线+十倍潜力)/龙头追踪/共振分析(3维度4级)/明日预案
- **个股详情7Tab**: 概况(23字段实时)/K线(蜡烛图+成交量)/资金流/信号评分(双雷达图)/板块共振/概念关联/明日预案
- **3个P0链路补全**: MA/BOLL后端实时计算+内存缓存 / DragonHead板块成分股组装 / 竞价→明日预案闭环
- **明日预案V2(2026-04-16)**: 复用强势排行_bull_rank数据管道, NextDayPlanEngine双策略引擎
  - 🔵 趋势买法: BullScore≥B + 站上MA20 + 资金/技术确认 → 1/3~1/2仓 → 止损=支撑×0.98
  - 🔴 激进买法: BullScore≥A + 技术确认≥1 + 放量 → 1/4仓 → 止损=入场价×0.97
  - ⛔ 不买条件: C/D级 / 跌破MA20 / 板块跌>2% / 5日净流出>3亿 / RSI>80 / 缩量涨停
  - 多源支撑压力: MA+BOLL+筹码密集区+前高+整数关口
  - 新增文件: signal/next_day_plan.py / api/index.js getStockNextDayPlan
- **新增TenbaggerModel**: 5维十倍潜力评分(市值/增速/估值/赛道/资金), 区别短线BullScore
- **共振算法公式化**: 方向一致(40分)+资金共振(30分)+生命周期(30分) → 强/弱/独立/逆势
- **风控增强3项**: 缩量涨停VR<0.8降级 / 次新股is_ipo_recent⚠️ / 一字板连板标注
- **morning_high/afternoon_low**: 暂传0，冲高回落用上影线判断(60%+覆盖)
- **工时**: 10.5天(约2周)
- **详情文档**: artifact `tenbagger_best_practice_v2.md`

## 开发路线图
- Phase 1-6: ✅ 全部完成(基础框架→联调部署)
- 牛股雷达数据驱动重构: ✅ 已完成(8新文件+4修改,集成测试通过)
- 非交易日date.today()修复: ✅ 已完成(resolve_latest_trade_date统一口径)

## 非交易日date.today()问题(2026-04-17修复)
- **核心原则**: tenbagger.py所有涉及trade_date的查询必须用 `resolve_latest_trade_date()` 而非 `date.today()`
- **原因**: 凌晨/周末/节假日时 `date.today()` 无对应数据，查询返回空结果
- **工具函数**: `app.core.data_date.resolve_latest_trade_date(session, date_col, requested=None)` — 自动回退到<=today的最大交易日
- **已修复函数**: prewarm_next_day_plan_snapshot / prewarm_anomaly_snapshot / prewarm_dragon_snapshot / _bull_rank / _tenbagger_rank / stock_score / stock_next_day_plan
- **SectorPersistence模型无lifecycle字段**: 用 `consecutive_days` 构建 "连续N天"

## 飞书推送
- Webhook已配置, 4时段复盘(8:30/11:35/15:10/20:00)
- 限频: 300s冷却+30条/小时

## AI模块
- MiniMax M2.7 (Anthropic兼容), 可选增强层, 牛股预测不用AI

## 板块营地(踩坑记录)
- **口径统一**: pywencai(loop=True), 行业257/概念389, 不展示申万
- **K线映射**: 概念95.9%/行业100%, kline_name: NULL=同名/非空=映射/空=无源
- **K线采集**: fast版并发5≈21秒, inner_code缓存7天, 3模式init/daily/repair
- **调度器**: 12任务, 快频10s/慢频30s/新浪5min/派生2min
- **8个Bug已修复(04-13)**: Lifecycle空表/概念名混乱/行业混入概念/涨停映射断裂/行业资金流0/pw_前缀/consecutive_days优先级/Lifecycle过度declining
- **核心**: 行业sector_code统一三级全名; Lifecycle不依赖涨停→基于Persistence; StockSectorMapping精确关联

## 前端架构
- 15页面, A股配色红涨绿跌, 深色背景#0a0e17, 移动端适配, 飞书H5可访问

## K线技术分析模块(2026-04-15 修复)
- **后端**: `spot.py` 内置纯函数指标计算(SMA/EMA/MACD/RSI/KDJ/BOLL), API返回全量时间序列
- **前端**: Detail.vue 专业级K线图: 主图(MA4线/BOLL3轨) + 副图(MACD/RSI/KDJ) + DataZoom + 指标切换RadioGroup
- **默认加载**: 800条K线≈3年, 最大支持2000条(8年+)
- **指标字段**: ma5/10/20/60, boll_upper/mid/lower, dif/dea/macd, rsi6/14, kdj_k/d/j, vol_ma5/10

## 强势排行+十倍牛股(2026-04-15 重写)
- **BullScoreModel**: 7维(动量20%/资金25%/技术20%/估值10%/基本面10%/规模5%/活跃度10%), 直接从StockSpot计算
- **_bull_rank**: 两轮评分(第1轮spot快速6维→Top200, 第2轮K线技术维度), ~8-10s
- **_tenbagger_rank**: 补全sector_days/sector_fund+5日资金+扩大筛选500只
- **circ_market_cap单位**: DB存的是亿, 不是元
- **SQLite优化**: WAL模式+busy_timeout=30s

## V2.2八因子融合增强(2026-04-15)
- **BullScoreModel v3.1**: 融合V2.2八因子系统到7维模型
  - 活跃度维度: 量比5级(极度缩量/缩量/正常/放量/巨量) + 换手率5级(死寂/低换手/正常/活跃/高换手) + 量价关系5态
  - 动量维度: 量价关系信号影响(放量上涨+3/缩量上涨-2/放量下跌-5)
  - 新增输出: volume_ratio_level / turnover_level / price_volume_relation / chip_signal(0-10)
- **ChipConcentrationAnalyzer V2.2增强**: 价格-成交量分布计算(10档分箱) + 集中度90%/70% + 形态识别(单峰/双峰/低位/高位/多峰) + 获利盘 + 支撑/压力位 + 信号强度0-10
- **大盘环境分析器**: service.py `_judge_market_environment` — 三维度(指数涨跌+情绪周期+涨跌停比) → strong/neutral/weak + 动态买入阈值(8.5/9.0/9.5)
- **spot API V2.2**: 返回量比等级/换手率等级/量价关系分类字段
- **stock_score API**: 集成筹码分析(ChipConcentrationAnalyzer), 返回chip详情(集中度/形态/获利盘/支撑压力位)
- **异动监控V2.2**: anomalies API返回sentiment上下文(情绪周期/大盘环境/封板率/连板高度)
- **前端增强**:
  - Detail.vue: 筹码分析卡片(8指标+支撑压力位) + 量化交易信号列表(分类标签)
  - tenbagger/Index.vue: 市场温度计增加情绪周期/大盘环境/封板率/连板高度卡片
  - RankTabContent.vue: 分页+排名序号+7维dimLabel+维度条
  - OverviewPrimarySections.vue: 大盘环境/买入阈值展示

## Bug修复记录(2026-04-15)
- **_calc_ema MACD 500错误(P0)**: 返回截断数组→等长数组(前NaN) + MACD对齐valid_start=25
- **流通市值显示0亿**: Detail.vue的circ_market_cap已是亿单位,无需/1e8
- **导出功能**: xlsx(SheetJS) + exportToExcel/exportToCSV, 强势排行+异动监控2处导出按钮

## 十倍潜力股跌停风控(2026-04-16)
- **TenbaggerModel v3.2**: 增加极端行情风控 — 跌停×0.4上限B / 暴跌×0.6上限A / 大跌×0.8
- **新增参数**: change_pct / is_limit_down, 两处调用(tenbagger_rank+stock_score)已同步传入
- **前端**: 评级列⚠️警告标记 + 导出列增加涨跌幅

## 强势排行代码评审修复(2026-04-16)
- **🔥 circ_market_cap单位修复(P0)**: 腾讯[44]返回元→入库时除1e8→DB存亿。leader_tracker同步移除重复/1e8
- **量价关系"缩量盘整"死代码修复(P0)**: 增加`abs(change_pct)<0.5 and volume_ratio<1.2`盘整判断
- **PB>10死代码修复(P0)**: 交换判断顺序 + PB[5,8]区间-3分 + 负PE亏损股-15分
- **跌停判定移除硬编码-9.5%(P1)**: 完全依赖is_limit_down参数
- **前端T等级颜色(P1)**: levelTagType + levelColor 添加 T: 'danger'
- **BullScoreModel scale钳位(P1)** + 高换手低涨幅阈值3%→1%
- **待修**: ~~NaN穿透防御 / spot.py EMA双循环 / _bull_rank RSI简单均值法~~ → ✅ 全部已修复

## 第二轮评审修复(2026-04-16)
- **NaN穿透防御(P0)**: 新增 `_safe_float()` 辅助函数, 入口统一转换, 防御NaN/None/inf
- **spot.py EMA双循环(P0)**: 删除双循环→单循环 + MACD DEA从有效段开始计算
- **_calc_rsi Wilder平滑法(P1)**: 从简单均值法改为Wilder递推, 与spot.py统一
- **or运算符吞0值(P1)**: 6处 `x or 0` → `x if x is not None else 0`
- **至此强势排行评审P0×5+P1×8全部修复完毕**

## 第三轮深度评审修复(2026-04-16)
- **RSI超买区加分→减分(P0)**: 70-80→-5 / 80-85→-10 / >85→-15 + risk_warnings推送
- **涨停判定硬编码9.5%→is_limit_up(P1)**: 统一用参数判定(支持20%涨跌停板块)
- **BOLL ddof不一致(P1)**: tenbagger.py两处 ddof=1→ddof=0(总体标准差,与spot.py/通达信一致)
- **小市值评分过高(P1)**: <30亿→70(流动性风险) / 30-80亿→85(最佳短线区间)
- **风控阈值区分20%涨跌停(P1)**: 新增is_20pct_board参数 → 创业板/科创板暴跌-14%/大跌-10%(vs主板-7%/-5%)
- **RSI<30超卖加分过乐观(P1)**: +5→+2, "超卖反弹"→"超卖观察"
- **十倍增速>200%低基数(P2)**: >200%→70分+标注"疑似低基数效应"
- **KDJ最少数据量(P2)**: 12根→20根K线(减少金叉/死叉误判)
- **十倍候选池市值过滤(P2)**: 增速查询增加circ_market_cap<500亿(排除大蓝筹)
- **至此三轮评审 P0×6+P1×13+P2×3 全部修复完毕**

## 第四轮深度评审修复(2026-04-16)
- **BOLL突破上轨超买位加分+风险提示(P0)**: 区分多头排列(+10)/非多头(+5) + 统一risk_warning"布林上轨附近注意回调"
- **SQL注入风险修复(P0)**: top300_codes直接拼SQL → 参数化查询(:code_0,:code_1...)
- **连板+5日动量双重加成(P1)**: 连板≥3时5日动量加成减半(+5)
- **跌幅-0.5%梯度跳跃(P1)**: 增加-0.5%~0%区间(30分)
- **资金面无数据虚高(P1)**: 低换手+缩量时基准25→15
- **RSI深度超卖区分(P1)**: RSI<25→+3分(深度超卖) / 25-30→+2分
- **bull_score增速>200%低基数(P1)**: 与tenbagger_model一致→70分+warning
- **高换手+放量上涨对倒风险(P1)**: 高换手时放量上涨加成+15→+5 + warning"疑似对倒"
- **十倍<20亿流动性(P1)**: <20亿→80分+warning / 20-50亿→95分
- **资金回退路径偏高(P1)**: else 30→20(无数据中性偏低)
- **KDJ交叉粘合区误判(P2)**: 增加K-D差值>5确认交叉(两处)
- **至此四轮评审 P0×8+P1×21+P2×4 全部修复完毕**

## MCP 工具注入策略 (2026-08-29 修复)
- **错误**: GPT 模型走 Codex app-server 时报 `dynamic tool name is reserved: mcp__claw_ashare__ashare_deployments`
- **根因**: DSH 的 `@deepseek-ai/dsh-mcp-client` 把工具以 `mcp__<server>__<tool>` 命名推给模型，Codex app-server 的 experimental `dynamicTools` 校验拒绝 `mcp` 或 `mcp__` 前缀（保留给 Codex 自己的 MCP 路由）
- **修复**:
  1. `~/.dsh/profiles/web/cordis.patch.yml` 删掉 `mcp-claw-ashare-readonly` insert（DSH 不再注入此 MCP）
  2. `~/.codex/config.toml` 加 `[mcp_servers.claw_ashare]` 让 Codex 自身加载 MCP server
  3. 验证: `codex mcp get claw_ashare --json` 确认 enabled，工具列表 `ashare_deployments/ashare_market_regimes/ashare_prediction_*/ashare_review_*/ashare_shadow_*/ashare_training_runs`
- **副作用**: DSH 此 profile 下 MiniMax-M3 也不再注入 Claw MCP（影响 M3 调试能力，但用户已确认 M3 仅修 bug）
- **GPT 上下文窗口**: Codex app-server 把 GPT-5.6-sol 限制为 372K tokens（effective 353.4K），不是 OpenAI API 标的 1.05M。新会话要避开长对话堆积，可用 `/compact` 触发 Codex auto-compaction
- **相关备份**: `~/.dsh/profiles/web/cordis.patch.yml.bak-2026-08-29`
- **回滚**: `codex mcp remove claw_ashare` + 恢复 cordis.patch.yml 备份行

## 模拟盘阈值放宽(2026-08-31 复盘修复)
- **症状**: active 账户总收益 -17.84% / 最大回撤 -25.28% / 胜率 49.5% / 盈亏比 0.57 / 平均持仓 1.6 天
- **根因**: 阈值剥头皮式紧(止盈 3.5% vs 止损 3.0%, 盈亏比 1.16), 加分钟级噪音敏感触发
- **修复** (`backend/app/config/settings.py` + `backend/app/api/v1/paper.py`):
  - `PAPER_AUTO_SMALL_STOP_LOSS_PCT`: 1.2 → 2.0
  - `PAPER_AUTO_STOP_LOSS_PCT`: 3.0 → 5.0
  - `PAPER_AUTO_OPEN_SEVERE_STOP_LOSS_PCT`: 4.5 → 6.5
  - `PAPER_AUTO_TAKE_PROFIT_PCT`: 3.5 → 5.5
  - `PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PROFIT_PCT`: 1.5 → 3.0
  - `PAPER_AUTO_BREAKEVEN_PROTECT_HIGH_PCT`: 0.4 → 0.6
  - `PAPER_AUTO_PULLBACK_FROM_HIGH_PCT`: 1.5 → 2.5
  - `PAPER_AUTO_NEXT_DAY_MIN_PROFIT_PCT`: 1.0 → 0.5
  - `PAPER_AUTO_MAX_HOLD_DAYS`: 2 → 5
  - **新增** `PAPER_AUTO_HARD_STOP_MAX_LOSS_PCT = 2.0`: 单笔硬止损触发时最大亏损 ≤ 总资产 2%, 防止大额穿透
  - `_short_sell_reason` 加 `hold_days >= 1` 门控给噪音敏感条件(跌破均价/开盘价/MA5/5分钟急跌/卖压), 避免开盘假动作打掉当天新开仓位
- **僵尸持仓**: 2026-04-30 买入的 3 只仓位(002466/001337/002371)在 account id=1 (closed) 上从未刷新, hold_days 字段停滞, 用 `scripts/cleanup_zombie_paper_positions.py` 按 stock_spot 最新价强制平仓释放资金
- **回测验证** (`scripts/backtest_sell_thresholds.py`): 70 个 round-trip, 新阈值 PnL +6,221 元 vs 旧阈值 +1,749 元 vs 实际基线 -10,029 元; 硬止损次数减半 (12 → 6)
- **影响范围**: 自动模拟交易逻辑全部, 不影响手动 /buy /sell 入口, 不影响风控链路
- **测试**: 974 个测试全部通过, paper 相关 6 个测试已更新预期值

## 模拟盘回撤开仓闸门总开关(2026-08-31 复盘新增)
- **场景**: 用户要"穿越牛熊持续评估算法胜率", 但当前账户 max_drawdown -25.28% 触发 paper 层 (2%/3.5%/5%) + risk 层 (15%) 多重熔断, 算法被永久锁死, 看不到穿越牛熊的胜率
- **新增配置** `PAPER_AUTO_DRAWDOWN_BUY_GATE_MODE` (settings.py):
  - `pause`    : 默认保守模式, 回撤 ≥ 2% 暂停开仓(老逻辑)
  - `cautious` : 跳过 paper 层暂停, 仍受 risk 层 15% 熔断保护
  - `unlimited`: 两层闸门全部跳过, **当前默认**, 只保留单笔仓位级风控 (止损/止盈/硬止损仓位上限/T+1/涨跌停/ST/停牌)
- **代码改动** (`backend/app/api/v1/paper.py`):
  - `_drawdown_buy_gate_bypasses_paper()` / `_drawdown_buy_gate_bypasses_risk()` 两个 helper 函数
  - `_is_drawdown_recovery_buy` 入口处: cautious/unlimited 直接返回 True
  - `_auto_buy_pause_reason` 入口处: cautious/unlimited 直接返回 ""
  - `_risk_check_for_buy` 在 unlimited 模式下: `ctx.max_drawdown = 0.0`, 让 MaxDrawdownRule 放行; 其他风控规则照常
- **保留的安全网** (unlimited 下仍生效):
  - 单笔止损 5% + 单笔仓位 ≤ 总资产 2% 的硬止损上限
  - 止盈 5.5% / 高点回落 2.5% / 时间止损 5 天 / T+1 / 涨跌停 / ST / 停牌 / 退市
  - `_run_auto_sells` 仍按全部规则管理离场
- **验证脚本**: `scripts/verify_buy_gate_modes.py` 用真实 active 账户跑三档对比
- **测试**: 新增 3 个测试 (test_buy_gate_mode_pause/cautious/unlimited); 既有 7 个回撤相关测试显式 `monkeypatch.setattr(..., "pause")` 保留老逻辑; 全套 978 个测试通过

## 模拟盘/牛股雷达/晋级预测 三链路选股一致性评审(2026-08-31)
- **链路A 模拟盘候选池**: `_paper_auto_buy_candidates` 7 来源 = next_day_plan + green_limit_reversal + underwater_reversal + ma5_pullback + anomaly_buy_point + icepoint_reversal + daily_participation
- **链路B 牛股雷达明日预案**: `prewarm_next_day_plan_snapshot` (bull_rank → NextDayPlanEngine), 页面与模拟盘共用同一快照缓存 (`_PLAN_CACHE`, key=`tenbagger-plan:{limit}:{version}`)
- **链路C 晋级预测**: `promotion_candidates` 完全独立 (news_catalyst/pre_board_probe/mainline_spread/auction_surge/second_board 五赛道), 用独立特征工程+逻辑回归, 与 A/B 无重叠
- **发现并修复的 Bug(P0)**: `_PAPER_ACTIONABLE_PLAN_TYPES` 只有 4 类 (strong_get_stronger/main_wave_confirm/trend/aggressive), 而 tenbagger `_NEXT_DAY_ACTIONABLE_STRATEGIES` 有 7 类 → **trend_pullback_buy / trend_breakout_buy / repair_followup_buy 三类在牛股雷达显示可执行但模拟盘直接过滤**, 已补齐对齐 (2026-08-31)
- **一致性与差异结论**: A 与 B 同源 (next_day_plan 来源=牛股雷达可执行预案子集, 额外加板块热度闸门+执行价值闸门); C 与 A/B 设计上就不同 (预测涨停概率 vs 短线买点), 不是 bug 而是职责分离, 但需在页面说明"晋级预测≠模拟盘买点"
- **当前市场宽度闸门影响**: `_classify_market_breadth` 要求 direct_buy_ok (上涨占比≥88%/平均涨幅≥1.5%/5日涨幅≥4%) 才放行直接买点; 8-11 后市场未达标 → 预案基本全是 watch/avoid, 模拟盘主要靠盘中候选 (水下翻红/MA5回踩/异动买点) 交易, 这是设计行为不是 bug
- **降级原因分布 (8-28 快照)**: 高标不直接买(50x) / 换手率0.0%不在区间(37x, 疑似高标数据缺换手) / 贴近BOLL上轨(24x) / 贴近20日高点(14x) / 涨幅10%不追高(9x) / 跌破MA20(9x) / RR不足(13x)
- **验证脚本**: `scripts/verify_buy_point_consistency.py` 三链路交叉对比

## 双策略并行模拟盘(2026-08-31 实施)
- **方案**: 一个前端页面 + 账户切换(策略A/策略B), 双账户独立跑 Champion/Challenger 对比
- **账户**: `PAPER_ACCOUNT_DEFAULT`(default, 策略A 现有链路, 初始5万) + `PAPER_ACCOUNT_PROMOTION`(promotion, 策略B 晋级二板, 初始5万)
- **策略B规则** (数据驱动, 晋级预测唯一正期望赛道):
  - 候选: `promotion_prediction_record` schedule快照 target_board=2, calibrated_probability ≥ 0.25
  - 信号日次日盘中确认: 09:35后, 涨幅 0~5.8% (不低开不追高), 非一字板, 量比≥0.6
  - 止盈 8% / 止损 6% / 持仓≤3天 / 单笔≤总资产20% (PAPER_PROMOTION_* 配置)
  - 跳过策略A的低吸性价比闸门(有自己的盘中确认), 卖出用 `_strategy_sell_params` 独立参数
- **后端改动**: paper.py `_get_or_create_account(account_name)` / 全部API加 account_name 参数 / `_short_sell_reason(params=)` 参数覆盖 / `_add_auto_log(account_id=)` / scheduler 双账户循环
- **模型改动**: paper_account.strategy 列 + paper_auto_trade_log.account_id 列 (Alembic 019 迁移)
- **前端改动**: api/index.js paper 系列函数加 accountName 参数; paper/Index.vue hero 区加 el-radio-group 切换
- **测试**: 新增6个策略B测试 (卖出参数/止盈8%/概率过滤/信号日校验/双账户隔离), 全套 984 通过
- **注意**: broker.py 直接调用 paper_buy/sell 需关键字传参 (account_name=..., db=...), 位置参数会错位

## 双策略模拟盘页面切换Bug修复(2026-08-31)
- **症状**: 前端切到策略B按钮后页面数据不变, 仍显示策略A(default)账户 (4.1万)
- **根因1**: 后端进程是旧代码(未重载), `_get_or_create_account` 无 account_name 参数, `account_name=promotion` 被忽略永远返回 default → **重启后端加载新代码**
- **根因2**: `_account_payload` 未返回 `strategy` 字段 → 补 `getattr(account, 'strategy', ...)` 或 account_name==promotion 兜底
- **根因3**: `paper_auto_logs/auto/status/auto/evaluation` 的账户过滤用 `(account_id IS NULL) OR (==account.id)`, 导致 promotion 账户混入历史 NULL 日志(9.4万条策略A日志) → 新增 `_auto_log_account_scope_filter`: promotion 只看自身日志, default 兼容历史 NULL 日志
- **关键**: 改后端后必须 `bash scripts/dev_services.sh restart` 重启, 前端走 Vite dev server(HMR) 无需重启
- **验证**: promotion 账户返回 5万/0胜率, 日志只 7条(自己), default 100条(历史); 984 测试全过

## 策略B auto/status 展示口径独立(2026-08-31)
- **症状**: 切到策略B后"自动执行"tab 仍显示策略A文案(买点来源=高胜率预案/最大持仓2只/止盈5.5%)——后端 `paper_auto_status` 的 signal_policy/position_policy/max_positions 硬编码策略A
- **修复**: `paper_auto_status` 在 account_name==promotion 时覆盖返回独立配置(strategy_label/max_positions=3/position_policy/signal_policy/short_trade_rules 全部用 PAPER_PROMOTION_*)
- **前端兼容**: 后端 position_policy 补 trial_pct/normal_pct/strong_pct/core_pct (均=PAPER_PROMOTION_POSITION_PCT), 前端 positionPolicyText 无需改即正常显示"20%-20%"
- **验证**: 策略B返回 buy_source="晋级预测二板赛道"/止盈8%/止损6%/持仓3天; 策略A保持原样(max_positions=2/止盈5.5%/持仓5天); 985 测试全过

## 双策略页面tab交互与评审(2026-08-31)
- **前端bug修复**: 切换账户后 `onAccountSwitch` 未重置 `activeTab`, 导致停留原tab(如从策略A交易记录切到策略B仍显示交易记录) → 加 `activeTab.value='account'` 回到账户概览
- **评审确认 tab 隔离**(前端loadData全部带 accountName.value): 账户概览/当前持仓/交易操作/自动执行/交易记录 全部按账户区分; 个股详情页(Detail.vue)是个股维度, 无 account 依赖不受影响
- **后端评估误买/误卖阈值适配**: `paper_auto_evaluation` 的 wrong_buys(-1.2小止损)/wrong_sells(3%浮盈过早卖) 原硬编码策略A阈值 → 按 account_name 适配(策略B用止损6%/止盈8%), 避免策略B展示误导
- **测试**: 新增 `test_cross_account_data_isolation` (买A不污染B, 持仓/交易/净值隔离) + `test_auto_status_distinguishes_strategies`; 986 测试全过
- **前端服务**: Vite dev server HMR 自动热更新, 改源码即生效无需重启; 后端需 `bash scripts/dev_services.sh restart`

## 五策略并行模拟盘(2026-08-31 扩展 C/D/E)
- **账户体系**: PAPER_ALL_ACCOUNTS = default(A) / promotion(B) / mainline(C) / auction(D) / tenbagger(E), 各 5 万初始资金
- **策略C 主线扩散首板**: promotion mainline_spread_start 赛道, 胜率50.1%最稳, 止盈4.5%/止损3.5%/持仓3天/单笔20%
- **策略D 竞价高开强攻**: promotion auction_surge_start 赛道, 胜率54.4%最高但样本少(482), 止盈5%/止损4%/持仓2天/单笔15%
- **策略E 十倍潜力中线**: tenbagger_rank 5维评分≥85, 中线10-30天, 止盈15%/止损8%/持仓30天/单笔15%
  - 独立 `_tenbagger_midline_candidates` + `_midline_sell_reason` (只硬止损/止盈/跌破MA20/时间止损, 无短线噪音卖点)
- **引擎泛化**: `_promotion_second_board_buy_candidates` → `_promotion_route_buy_candidates(db, account_name)` 按账户路由赛道/概率阈值/涨幅确认区间; `_source` 改为 `promotion_{account_name}`
- **买入循环**: promotion_* 与 tenbagger_midline 候选跳过策略A低吸闸门, 各自独立仓位上限
- **auto/status**: `_strategy_status_override(account_name)` 统一返回 B/C/D/E 独立展示口径(策略A None)
- **账户策略标识**: `_get_or_create_account` strategy=account_name (在 PAPER_ALL_ACCOUNTS 内); `_account_payload` 兜底同上
- **测试**: 新增4个 (C/D候选路由隔离/中线卖出无噪音/十倍评分过滤/五账户status), 990 全过
- **调度器**: 盘中+收盘循环 PAPER_ALL_ACCOUNTS 五账户依次执行

## 五策略独立性与映射审计(2026-08-31 复查)
- **数据隔离确认**: 6账户(1 closed default + 5 active) 持仓/交易/净值/日志/评估 各表按 account_id 隔离; default 正确映射到 active 的 id=2 (跳过 closed id=1)
- **前端→后端映射**: api/index.js 7接口全部带 accountName; paper/Index.vue loadData/买卖/切换全部用 accountName.value; 前端切换 tab 重置到账户概览
- **运行时路由**: run_paper_auto_trade 按 account_name 分流 — default→_paper_auto_buy_candidates / promotion/mainline/auction→_promotion_route_buy_candidates / tenbagger→_tenbagger_midline_candidates, 验证无串扰
- **发现并修复 bug**: `empty_reason`(候选为空文案) 只判断 promotion, 导致 mainline/auction/tenbagger 空时显示策略A文案 → 新增 `_strategy_empty_reason(account_name)` 按账户返回对应文案
- **测试**: 新增 test_strategy_empty_reason_per_account, 991 全过

## 五策略回放与参数寻优(2026-08-31 落地)
- **P0修复(成交落账隔离)**: broker.place_order 原硬编码 default, 导致 B/C/D/E 自动成交全记入 default 账户 → SubmitOrderCommand 携带 account_id → BrokerOrderRequest.account_name → paper_buy/sell 按账户路由; _pre_trade_risk_check 也按账户路由风控
- **P1修复(策略E MA20死代码)**: _build_short_sell_context 原来只算 ma5, 改为拉21根K线补 ma10/ma20; _midline_sell_reason 加 params 参数覆盖
- **回放引擎** `scripts/replay_five_strategies.py`: 真实信号快照→次日开盘买入→逐日调用实盘 _short_sell_reason/_midline_sell_reason 模拟止盈止损; T+1规则(买入当日不评估卖点); 复用 PerformanceAnalyzer 思路; 佣金万3+印花税千1
- **轨道1结果**: A仅2可执行买点/ B 1088信号胜率45.6% +4008元/ C 4230信号胜率44.4% +32552元/ D 9信号无统计意义/ E 52信号胜率29.4% -2284元
- **轨道2寻优** `scripts/replay_threshold_scan.py`:
  - B阈值0.25已最优(升阈值反而变差, 推翻此前分档统计推断); 止盈8→12%/止损6→5% 总盈亏+157%
  - C阈值0.20→0.40(+42%); 止盈4.5→8%/止损3.5→2.5%
  - E全线亏损(15组合全负), 建议暂停诊断信号质量
- **报告**: `outputs/paper_strategy_optimization_20260831.md` (含严谨性边界: 日线级近似/样本内最优风险/需观察3-6月后重验)
- **测试**: 993 全过 (新增 P0路由测试/P1 MA20测试/中线params测试)

## 五策略参数应用 + 策略E诊断(2026-08-31 落地)
- **B参数已应用**: 止盈8→12% / 止损6→5% (阈值0.25保持); `_strategy_status_override`/`_strategy_display_meta` 文案改动态引用 settings
- **C参数已应用**: 阈值0.20→0.40 / 止盈4.5→8% / 止损3.5→2.5%
- **D保持**: 仅9信号统计无意义
- **A说明**: 回放仅覆盖明日预案(2个可执行买点), 主要交易来源是盘中快照需分钟数据; 真实画像用历史成交(113笔/胜率48.7%/盈亏比0.57)
- **E已暂停** (`PAPER_TENBAGGER_ENABLED=False`) + 深入诊断:
  - 评分档位倒挂: 80-85分 r10d +1.05% vs 85-90分 r10d -4.54% (评分不预测短期上涨)
  - 持有期越长越亏: 20天-10.65% / 30天-13.65% / 60天-22.2%
  - 15组止盈止损全亏 → 信号设计缺陷非参数问题
  - 根因: 十倍评分高分票往往已涨过一波, 买入处于回调期; 评分与10-60天收益负相关
- **测试**: 993 全过 (B止盈测试改动态阈值 / C概率测试改0.45 / E测试显式启用开关)

## 策略E重建验证结论(2026-08-31 最终)
- **用户选定方向**: 中盘50-200亿 + 回踩MA20确认 + 缩量企稳
- **已实现** (settings + paper.py): PAPER_TENBAGGER_MID_CAP_MIN/MAX=50/200亿, PULLBACK_MA20_MAX_DIST=3%, PULLBACK_FROM_HIGH=5-20%, SHRINK_VOLUME=0.8; 回踩判断用信号日收盘价(最近K线)而非实时spot
- **验证结果(诚实报告)**: 该组合在十倍评分池内**无法修复亏损**:
  - 42个评分≥85候选中35个市值<50亿(池子83%是小盘) → 中盘高分候选仅7个且同样亏损(-11.6%)
  - 回踩确认通过的候选仅1个且亏损(-28.5%)
  - 4组质量过滤(增速>0/PE<150/非暴跌/中盘)全部亏损(477→455→438→93样本, 平均20日-3.8%~-4.5%)
  - 五重证据: 评分档位倒挂 + 持有期越长越亏(20天-10.6%/30天-13.7%/60天-22.2%) + 15组止盈止损全亏 + 4组质量过滤全亏
- **根因**: Tenbagger 5维评分(市值/增速/估值/赛道/资金)与10-60天未来收益无预测力; 高分票多为"已启动的高估值小盘"(净利润下滑/PE极高常见), 买入即回调期
- **最终处置**: E保持暂停(ENABLED=False), 重建代码保留为框架(注明已验证无效), 不建议在评分模型重建前开启
- **测试**: test_tenbagger_midline_candidates_score_filter 重写为验证中盘+回踩过滤(600100通过/600300未回调拒绝); 993全过
- **诊断脚本**: scripts/diagnose_tenbagger.py (评分/市值/月份/持有期四维) + scripts/validate_tenbagger_rebuild.py (多组规则对比)

## 策略E重建为连板高标接力(2026-08-31 最终版)
- **用户高标股调查结论**: 哈药股份(+165%)/百合花(+137%)/爱丽家居(+126%)/传智教育(+47%)/风华高科(+53%) 全部进过十倍候选池但评分68-81<85被拒
  - 扣分元凶: 估值维度(40-60分) — 高标股天然高PE/亏损(炒预期), 模型惩罚了市场最热的票
- **特征重要性分析**(50258样本): 市值/增速/估值3维与未来20日收益**负相关**(高分更差), 赛道/资金弱正相关, 总分负相关(30分档-2.84% vs 90分档-19%)
  - 十倍评分模型5维全部无正预测力 → 旧E全线亏损的根因
- **正期望信号发现**: 连板≥4高标次日买入持有5日 +3.13% 胜率55.6%; 剔一字板后+1.25%/54.1%; 加-8%止损后+2.35%(截断-35%尾部)
  - 2-3板是负的(-1%), 4板+2.39%, 5板+5.00% → 连板数是最强特征
- **重建实现**: E改为"连板高标接力" — limit_up_pool连板4-8板 + 封板资金≥1亿 + 炸板≤2次 + 已开板可交易(price<limit_up) + 止盈12%/止损8%/持仓5日
- **回放验证**: 旧E(十倍评分) -2284元 → 新E(高标接力) **+57795元** 胜率49.7% 盈亏比5.12; 抓到哈药(7/16五板后+15.4%)和传智教育(8/05八板后+12.3%)
- **P1 bug顺带修复**: 策略B/C/D/E候选的一字板检查用错字段 limit_up_price→limit_up (StockSpot无limit_up_price字段), 原一字过滤一直失效
- **测试**: 993全过 (E候选测试改为验证连板过滤; midline卖出改为高标接力参数; status/empty文案更新)
- **实盘验证**: 8-28当日5只连板≥4高标全部涨停封死(现价=涨停价) → 被"已开板才买"过滤, 等开板; 逻辑符合交易纪律
- **回放引擎**: E信号源改为_limit_up_pool连板≥4; E配置止盈12/止损8/持仓5日; 次日一字板跳过
- **脚本**: scripts/analyze_tenbagger_features.py (特征重要性) + scripts/rebuild_tenbagger_model.py (多条件组合验证)

## 回放引擎严谨性审计 + E结论修正(2026-08-31 用户质疑后)
- **用户质疑**: 回放是否只针对列举高标股 → 检查确认回放信号是全市场 limit_up_pool 连板≥4 (263信号/133只/86天), 非拟合
- **但发现回放引擎3个缺陷并修复**:
  1. **20%涨跌幅**: 688/300/301板块用10%判断一字板 → 按代码前缀区分 10%/20%
  2. **ST未过滤**: *ST原尚/*ST椰岛等进入回放 → _simulate_hold 开头加 ST 过滤 (实盘risk层也会拦)
  3. **持仓日期错位**: 停牌导致 idx+5 跳变数周 → 属数据特性(实盘同样停牌), 非bug
- **修复后 E 严格检验**: 105成交(剔除ST/20%板后), 剔除Top3赢家后仍 +1.31%/胜率49% → 不依赖少数赢家, 信号质量真实
- **E处置(08-31 晚已反转)**: 该结论被参数扫描推翻 — 补上实盘同款封板过滤后 79 高质量信号下基线即 +2.82%, 扫描最优 18/6/3 (4.41%/58.6%), E 已启用。详见下节"五策略全部启用"。
- **诚实修正**: 此前 +57795 回放包含ST/20%板错误, 修复后 +59403 但口径更严谨; 两者差异在卖出规则(回放引擎用止盈12%+MA20+到期5日, 严格脚本仅持有5日+8%止损)
- **测试**: 993全过; 回放引擎修复不影响 B/C/D 结果

## 五策略全部启用 + E参数扫描最优解(2026-08-31 晚 用户拍板)
- **用户决定**: "5个策略都要启用,包括E,调优止盈策略最优解" → E 从暂停改为启用
- **为何这次能测出正期望(上次测不出的根因)**: 回放信号未应用实盘同款过滤!
  - `_load_highboard_signals` 补上 seal_amount≥1亿 + break_count≤2 (实盘 `_tenbagger_midline_candidates` 有, 回放没有)
  - 效果: 254 低质量信号 → **79 高质量信号**; 高质量信号下基线 12/8/5 就是 +2.82%/胜率55.2%
  - 上一轮混入 175 个一字/ST/封板不足低质量信号把期望拉平 → 结论反转
  - 另修: 回放 ctx 补 ma5/ma10/ma20 (实盘 `_build_short_sell_context` 有, 回放缺 → 跌破MA20 规则回放从未生效)
- **参数扫描**(scripts/replay_tenbagger_scan.py): 192 组合 × 三重防过拟合
  - 网格: 止盈 8-25 × 止损 5-10 × 持仓 3-10 × MA20 开关
  - 留一法: 剔任意一笔仍正; 剔Top3/剔Top5: 稳健组合仍正; walk-forward: 前后半段都正
  - **最优 18/6/3**: 均收 4.41% 胜率 58.6% 盈亏比 1.46, 剔Top3后 2.16%, 剔Top5后 0.79%, 留一 3.9-4.8%, walk-forward 前半5.35%/后半2.87%
  - 基线 12/8/5 对比: 2.82%/55.2%, 剔Top5 转负(-0.50) → 依赖赢家
  - 止盈 18% 是稳健拐点: 15-20% 平滑平台, ≥25% 或不止盈时剔Top5 转负 (止盈是正贡献)
  - 止损 6% 最优 (8/10% 更差); 持仓 3 日最优 (快进快出); MA20 开关无影响 (3日内止损先触发, 保留作保险)
- **配置落地** (settings.py): `PAPER_TENBAGGER_ENABLED=True`, `PAPER_HIGHBOARD_TAKE_PROFIT_PCT=18`, `STOP_LOSS_PCT=6`, `MAX_HOLD_DAYS=3`
- **回放引擎同步**: E 配置 12/8/5 → 18/6/3; ctx 补均线; 封板过滤
- **测试**: 1003 全过 (93 paper + 910 其他); MA20 用例调整(-5.5%> -6%止损仍触发MA20)
- **后端重启生效**: E 账户(id=6) enabled=True, 今日 2 只高标候选(海鸥住工5板/万向德农4板)进盘中确认
- **最终回放**: E 29笔 胜率58.6% 均收4.57% +9791元 最大单亏-571.5; A/B/C/D 不受影响
- **报告**: outputs/strategy_E_param_scan_20260831.md
- **风险提示**: 29笔样本仍小, limit_up_pool 仅自2026-04, 样本内结果 → 实盘观察期继续验证

## 断板反包策略回测(2026-08-31 晚, 用户要求第6策略候选) — 结论: 不上线
- **用户动机**: 金牛化工(7/17-8/28 +97%)等断板反包股是5策略盲区; 用户认为"很多票一个月/半个月涨50%+"
- **回测设计**: scripts/replay_break_reversal.py, 全市场 2018-01~2026-08 K线 (5019只/850万行),
  封死=收盘≥涨停价×0.995 (炸板不算连板), 断板反包=今日封死+昨日未封死+最近一棒连板≥N+断板≤G+放量≥V+非一字,
  复合收益规避除权污染, |change_pct|>30% 数据护栏, T+1/佣金万3+印花千1/次日开盘买入/逐日检查
- **三个关键结论**:
  1. 简单断板反包(连板≥2): 2644笔 均收-1.81% 胜率33.8% 每年都负 → 稳定负期望
  2. 严格子集(连板≥3+断板≤3+断板日≤-5%+放量≥1.5x+封死): 12/8/3 均收+1.30% 胜率48.2% 剔Top5+0.67%
     但年度方差大: 2020 +6.65% / 2021 **-5.38%** (风格风险, 情绪过滤无效); walk-forward 前半剔Top5 -0.99%
  3. **20日持有视角决定性证据**: 信号后持有20日 平均-8.61% 中位-13.78%, 涨≥50%仅3.1% (7笔),
     跌≥10%占59.8%, 每年20日均收都负 → 用户观察的"一个月涨50%"是**幸存者偏差**, 无可复制统计特征
- **处置**: 不上线第6策略; 若用户坚持可小仓位观察(8/12/3), 但必须理解风格风险;
  若要继续探索需板块/资金/题材共振, 而非单一K线形态
- **脚本**: scripts/replay_break_reversal.py; **报告**: outputs/strategy_F_break_reversal_20260831.md

## 前端策略辨识度(2026-08-31)
- **背景**: B/C/D/E 新账户都是空账户(5万/0%)数字全同, 切换看不出是否成功
- **后端**: `_account_payload` 新增 `strategy_label`/`strategy_desc`/`strategy_short` (来自 `_strategy_display_meta`), account 接口统一返回策略元数据
- **前端** (paper/Index.vue):
  - 策略主题色: A蓝(#007aff)/B紫(#a855f7)/C绿(#10b981)/D橙(#f59e0b)/E红(#ef4444)
  - hero 下方新增 `strategy-banner` 横幅: 徽章+标签+描述, 按策略主题色渲染
  - `STRATEGY_THEMES` + `currentStrategyTheme`/`currentStrategyMeta` computed
  - 切换账户时 `notifySuccess("已切换到 策略X · ...")` 即时反馈
- **验证**: Playwright 切 E/C 均显示正确横幅标签+主题色+toast; 991 测试全过, 前端构建成功

## 晋级预测历史面板真值/召回/校准修复(2026-09-01)
- **真值与时点**: 历史标签统一走日期生效主板阈值；改革前 T/T+1 的 4.5%~6.2% ST 歧义样本保守隔离（500日共65887条/4.4016%，并非准确ST识别）；快照真值排除 `LimitUpPool.quarantined`。
- **未来函数修复**: 历史参考概率改成固定 Beta 先验 + 只含更早日期结果的 expanding prior；走步验证禁止 `step_days < validation_days`，避免时间外样本重复计数。
- **候选召回**: 预筛 MA20 改为真实20日均线；池内召回与全市场召回分开报告。300/450/600 日候选对照后默认统一为450（training/API/CLI/构建器/前端）。
- **450档结果**: 500日面板、250独立验证日、13折；池召回33.4982%，AP 0.086029 vs 历史参考0.044999，Brier 0.033494 vs 0.034238；Top12=387命中/12.90%/全市场R3.2450%，Top30=844命中/11.2533%/全市场R7.0770%。
- **概率校准**: Platt 改用 Newton+回溯，ECE降至0.003103、均值概率3.5722%接近基准率3.5511%；但相对近常数参考的门槛为0.001595，仍诚实失败，不放宽阈值。
- **治理结论**: 历史结果固定 `pretraining_only`，生产 Champion 不变。历史 `hist_*` 特征尚未同口径物化到不可变在线快照；当前不可变台账仅2108行/3运行/1交易日，未满30日，不得影子晋级。
- **报告**: `outputs/promotion_challenger_validation_20260901.md`；接口口径补充在 `docs/promotion-api.md`。
- **验证**: promotion modeling 17通过；promotion相关229通过；后端全量1083通过（1个第三方弃用警告）；前端生产构建通过；Playwright晋级页实时API一致性1通过；重启后OpenAPI默认候选数为450、生产身份仍为 `promotion_v20260829_27_governed`。

## 每日复盘盘后决策台修复(2026-09-01)
- **后端契约**: 快照升级为 `daily_review_workbench_v2` + `daily_review_decision_view_v1`，新增复盘结论、下一交易日指引、场景预案、风险证据与 `next_trade_date`；历史快照由 API 兼容生成视图。
- **日期真值**: 禁止未来复盘、阶段与 `as_of_at` 越界；盘后必须使用复盘日同日收盘行情；盘前/盘中校验本地交易日历，避免周末/休市日和未来数据穿透。
- **数据诚实性**: K线/资金/情绪/基本面缺失值不再伪装成 0，K线涨跌覆盖率低于80%视为源不完整；证据不足时结论降级为“市场证据不足”。
- **GPT报告**: 生成必须显式绑定不可变 `snapshot_id` 与 `data_version`，失败保留上一份成功报告；未配置时前端明确禁用，不影响确定性复盘。
- **前端**: `daily-review/Index.vue` 重构为盘后决策台，修复菜单图标、日期字段、单位/百分比、历史版本、只读回放、自动化与告警说明，并完成移动端适配。
- **验证**: 最终快照 id=15（复盘日/行情日 2026-08-31，下一交易日 2026-09-01，K线涨跌覆盖率100%）；聚焦测试21通过、后端全量1083通过、前端生产构建通过、Playwright每日复盘1通过；服务健康。未改交易模型权重或下单逻辑。

## 收盘真值、情绪质量与模拟盘版本隔离(2026-09-02)
- **收盘真值**: `_spot_to_kline_fill(finalize_close=True)` 逐股要求 `StockSpot.updated_at >= 15:00`，重建前清理当日旧 `tencent_close`；`_close_snapshot_health` 同时阻断 fallback 和陈旧终值。2026-09-02 手工重跑结果：5204/5219 canonical、完整率99.7126%、fallback=0、stale=0。
- **情绪口径**: 外部 `sentiment_score` 固定0～100，当前版本 `breadth_index_quality_v3`；买入遇 missing/degraded/stale 必须失败关闭，卖出不受阻断。内部离散周期点数单独保留，不能直接持久化为百分制。
- **数据源健康**: 完整率0不能用 `or 1.0` 吞掉；失败默认记录0并暴露 error_msg。基金流/板块映射必须保留 `source/source_version/observed_at`。迁移 `023_health_completeness` 纠正历史 down/degraded + 100% 假健康记录。股票状态四查询全空/类型异常时必须在改库前失败关闭；批量替换使用 SQLAlchemy `delete(...in_(codes))`，不要再手写依赖未导入 `text` 的SQL。
- **竞价质量门**: 可执行竞价证据至少需要同股两个价格、量、额、量比均为正的帧；单帧价格或普通开盘价只能覆盖展示，不能放行 D 路线。路线门只依赖输入数据，`promotion_prediction_record` 是闸门后的输出，禁止形成循环依赖。
- **版本隔离**: `strategy_version` 贯穿下单、排队、持仓、成交、自动日志；排队时冻结版本，版本变化后禁止补成交；跨版本/未标版本禁止加仓。Challenger 证据和最近动作只统计当前 route/strategy version，账户成立以来指标可跨版本但必须标注。
- **策略状态**: B/C/D/F Champion 自动买入暂停，A/E 保留原严格门禁；B2/C2/D2/F2 完全隔离且仍需20个独立交易日、100 confirmed、收益/超额/前后半段/去Top5/MAE/集中度/账户回撤≤15%和人工评审。
- **前端语义**: 模拟盘所有价格、买卖、成交、持仓与 Challenger 执行必须持续显示“本地模拟/模拟价格/不连接真实券商”，并展示版本和完整晋级清单。
- **验证**: Alembic head=`023_health_completeness`；后端分组全量1121+61+6=1188通过；前端生产构建通过；模拟盘Playwright 5通过。B2/C2/D2/F2 当前版本 confirmed/evaluated 仍为0，不得宣称策略有效。

## 上午复盘续作：C3 主线首板前向采证(2026-09-03)
- **路线身份**: 新增 `c3_mainline_first_board` / `c3_mainline_first_board_v1`，从 2026-09-04 下一完整交易日激活；非法激活日 fail-closed，生产扫描强制服务器当天时钟，函数不保留历史回填参数。
- **完整分母**: 每帧先验收可交易池行情、pywencai 行业/概念映射及当日 `SectorPersistence` 覆盖均≥95%，并把映射缺失、持续性缺失、非因果标签、低于结构门槛的数量写入 `universe_audit`。结构池要求近5日无涨停记忆且板块强度≥25/涨幅>0/资金流>0/涨停≥1；资格池再要求板块强度≥50/涨幅≥0.5/涨停≥2、个股涨1%~7%、量比≥1、卖一有效且未触板。
- **确认与对照**: 现价≥VWAP、距高点回撤≤1.5%、相对板块≥+0.5ppt、盘口失衡≥-0.10，3帧/60秒/帧隙≤75秒。`control` 只取同资格未完成确认者且不看收盘结果；生成前还须≥300个健康全市场帧（上午≥180、下午≥120），上午首帧≤09:35/末帧≥11:25、下午首帧≤13:05/末帧≥14:30，且两段各自帧隙≤180秒，否则 `session_blocked`；单个午后恢复帧不能冒充完整下午观测。
- **收盘与结算**: 20:30后且结构分母100%具有正式日K才贴收盘标签；只接受 `ths` 或有15:00后终场spot佐证的 `tencent_close`。确认组和未确认对照分别结算T+1/T+3/T+5，缺日或价格链断裂右删失；同日首板命中只做漏斗诊断，不是晋级指标。
- **交易隔离**: C3 不在 Challenger 账户映射中，不创建现金/持仓/NAV/成交；账户回撤标为本证据版本“不适用”并排除统计门槛，既不伪造0%通过也不永久卡死证据评审。未来若进入隔离模拟必须另立版本并人工评审；A–F Champion及真实券商边界不变。
- **页面/审计**: 策略C页可切换C2/C3，两版样本不混；C3显示“只采证·不撮合”、对照提升和同日漏斗。14:27只读就绪审计：3002/3002行情、映射、板块截面覆盖，结构分母214、资格0、单帧确认0，当前涨停池44/首板34，激活前C3事件/账户/成交均0；该快照不是前向证据，不据此放宽阈值。
- **验证与产物**: 后端核心、交易与调度关键链路回归224通过（C3/Challenger聚焦37），前端构建通过，Playwright桌面/移动2通过。说明见 `outputs/morning_review_20260903_1148/首板C3前向采证实施_20260903.md`，只读审计见同目录 `首板C3链路就绪审计_20260903.md`/JSON，页面截图 `策略C3前向证据页面_20260903.png`。

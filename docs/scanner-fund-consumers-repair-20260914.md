# Scanner 剩余资金消费修复（2026-09-14 / round5）

## 范围与证据

已重读 AGENTS、.workbuddy/memory/MEMORY.md 资金说明及现有源码。不是重做父阶段已经修好的 scan_market / score_stock 五会话窗口、capital_anomaly 连续 run 或 tenbagger 冻结 cutoff。

本次确实发现：
- _assemble_sector_stocks、_assemble_many_sector_stocks：查询 <= 请求日期的全局最新 FundFlow 日期，只取金额并把缺值转 0。
- analyze_resonance：逐板块重复读取个股 <= 请求日期最新 FundFlow 金额，缺值转 0 后落入 +5 分。
- DragonHeadResult 原来再次把未知金额转 0；模型直接数值输入也没有有限值防御。
- SectorPersistence 只有日期和板块资金（亿），没有资金来源/版本/源收观时钟；不能声称整个共振分可执行。

只读代码和隔离测试数据库，没有读取生产业务 API/数据库，没有下单、部署、迁移或改写历史。旧复盘/预测证据未改。

## 实现（5个代码/测试文件 + 本文）

1. backend/app/signal/anomaly_scanner.py
   - 仅上述三函数及必要 import；增加可选 keyword-only as_of_at，单次调用共用 cutoff。
   - current 数值只用现有 load_current_main_fund_map；限定请求日期、真实来源版本、有效金额/比例、合法源收观时钟及原新鲜度阈值。
   - 缺失/过期/未来/非法来源/错误日期为 None，并保留 main_fund_status；真实 0 和负数保持原单位（元）。
   - 日期展示通过现有 load_latest_main_fund_display_map 单独加载请求日，不回退上一日、不制造历史 PIT。main_fund_display 的 purpose=display_only，绝不回填 current。
   - 共振资金仅读取一次，移出板块循环。缺个股可用证据或缺同日有限板块金额，资金分为 0；完整有效数值分支仍是 30/10/15/5，方向分/生命周期分/等级门槛不变。
   - 共振整体 purpose=research_only、fund_contract_version=strict_current_dated_display_v1；板块同日数值标记 dated_unclocked，不是实盘可执行证据。旧日期板块资金保留来源日期但 current sector_fund=None。
   - 没有所属板块时明确 not_evaluated；不包装成测得零资金。

2. backend/app/signal/dragon_head.py
   - 复用 main_fund_values_valid 做必要输入防御，显式非 ok 状态不能通过大金额或 display_only 获得资金加分。
   - 缺值不进入资金分位排序；孤立有效值不能令无效股票借单元素分位获得100。
   - 输出金额可空并携带状态/cutoff/独立展示证据（深拷贝）。
   - 纯模型既有数值输入接口仍兼容，标记 numeric_input_only，不能当作来源验证证明。
   - 没改辨识度、趋势、补涨、可交易性权重/公式/门槛。严格输入修正可能改变排名和看A做B候选，未自动 Champion 或交易。

3. frontend/src/views/tenbagger/components/ResonanceTabContent.vue
   - 局部增研究用途提示、个股当前资金/状态与独立日期快照/源时点。
   - 当前字段非 ok 时即使意外附带金额也显示 --；零显示 0.00亿，负数保留符号。
   - 明示盘后日期快照不是当前交易资金、也不是确认收盘值。没有改公共 API/styles 或其他页面。

4. backend/tests/test_scanner_fund_consumers_20260914.py
   - 新53项：三消费者一致性，真实正/零/负，缺源、缺版本、缺比例、三钟分别缺失、过期、未来、旧日、源钟跨日、缺行；同日板块分值原分支、旧板块和盘后降级；dragon未知/非有限/非法输入/证据复制隔离。
   - 复用已有隔离 SQLite tmp_path fixture，屏蔽 provider HTTP；断言读取后金额和行数没有改变。

5. frontend/e2e/scanner.fund-consumers.spec.cjs
   - 原 5173，业务 API 全拦截、WebSocket关闭，无生产业务 GET。
   - 交互切到共振 tab，验证零/负/过期/未知及独立研究快照，pageerror 与写请求均为空。
   - 输出独占 outputs/repair_validation_20260914_round5/fund_consumers，不清其他 test-results。

## 验证

后端固定环境：PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false /usr/local/bin/python3.11 -B -m pytest ... -q -p no:cacheprovider。
- 初跑44项：43通过、1失败（测试错误把可解析数值字符串视非法；共享工具允许解析数值字符串）。修正为真正非法字符串断言，未改生产规则。
- 第一批联合四文件：271 passed / 23.93s。
- 最终全资金 glob + anomaly + promotion news evidence + nextday detail/engine：940 passed / 52.28s（包含本轮53新测）；仅 python_multipart 既有弃用提示。
  实际参数：tests/test_*fund*.py tests/test_tenbagger_anomaly_logic.py tests/test_promotion_news_evidence.py tests/test_stock_next_day_plan_detail.py tests/test_next_day_plan_engine.py。
- 一次命令把现有 test_next_day_plan_engine.py 误写为 test_stock_next_day_plan_engine.py，pytest exit4、未运行测试；已通过 glob 核实并重跑正确文件。
- npm run build：成功，7.07s；仅既有大 chunk 提示。
- npx playwright test e2e/scanner.fund-consumers.spec.cjs --output=../outputs/repair_validation_20260914_round5/fund_consumers --workers=1：1 passed / 1.9s。
- 截图已实际视觉核验：outputs/repair_validation_20260914_round5/fund_consumers/e2e-scanner.fund-consumers-5dbdb-and-dated-research-evidence/resonance-funds.png；零、负、未知与日期快照清楚分离，未见遮挡。

## 父集成建议及仍未证实事项

已报告父，不越权修改 API：
- tenbagger.py 的 DRAGON_SNAPSHOT_VERSION 目前 v5。建议提升新版本（例如 v6_strict_current_fund_v1），避免旧来源口径排名缓存混用；不改旧缓存/历史业务记录。
- _serialize_dragon_result 当前不输出资金字段。建议紧邻 change_pct 添加：
  - main_net_inflow = result.main_net_inflow
  - main_fund_status = result.main_fund_status
  - main_fund_decision_at = result.main_fund_decision_at
  - main_fund_display = copy.deepcopy(result.main_fund_display)
  并补 API nullable/zero/source-clock serializer 测试。副本已是最小自有JSON证据，不是ORM/运行时对象。
- 本轮没有恢复板块资金来源时钟（模型无该证据），共振只能做日期研究；不能将 research_only 当交易放行。
- 历史 FundFlow 是可覆盖日行；dated display 不构成历史决策 PIT，无法补证旧时点资金，也不宣称恢复9/14覆盖率。
- 现有行情/板块方向/生命周期仍可能来自非同一时点，未扩大修复；整体研究提示不能代替未来完整多源同步审计。
- 未修改父保护段及 scanner 以外其他资金消费；不宣称全系统资金消费已经闭环。
- 没有部署，所以生产运行结果不在本轮验证声明内。

## 源码冻结指纹

- anomaly_scanner.py: 174a1fb157d88b1032aff0b23c27152ab786a27d55a952fe8c6e3619eb7ec06f
- dragon_head.py: 4d2d9c51d6ebfd772362c735b0764daf573c8447450d0ef3314bdd34aaa0498f
- test_scanner_fund_consumers_20260914.py: c82e2270a69181a1d7fb9854496581e4f0e9614109a987f454760a539b0007e5
- ResonanceTabContent.vue: c878d11853075534b9f0ec05334a259f69ba0eeb788a802418ba77b4fd1a39a4
- scanner.fund-consumers.spec.cjs: 05d5c9be00d1784d77ee21a715e52ca4c10b37550511ac72480a0165efab21e9

指纹是本代理冻结时工作树文件（可能包含先前并行改动），不是 Git 提交声明。

## 父集成追加（源文件哈希以后续父manifest为准）

父已阅读三消费者/模型/页面实现并实际看过截图：零0.00亿、负-1.20亿、stale/unknown的--、独立日期快照+2.30亿及源时点清楚分离，无遮挡。
- tenbagger局部提升DRAGON_SNAPSHOT_VERSION为v6_strict_current_fund_v1；serializer输出可空main_net_inflow、main_fund_status、main_fund_decision_at及独立深拷贝main_fund_display。
- 三消费者默认fund_decision_at改为首次await前捕获，请求日默认取该冻结时钟日期，避免前置行情/板块查询耗时把更晚资金收证纳入本次最初截止。
- 新增test_dragon_fund_api_contract_20260914.py：零/正/负/缺失/过期API与深拷贝，三方法实际前置await推进时钟且较晚observed不可被追认。
- 父初轮集成测试74 passed / 3 failed，失败为新测试误把display-only的available=false/future诊断对象期待为None；已对照共享main_fund_display_evidence改成断言available=false、clock_status=future、金额None，未放松业务门禁。
- 父最终7文件联合332 passed / 41.13s，包含三消费者、API序列化/截止、既有5会话窗口、质量审计冻结和新闻/K线雷达回归。
- 父最终前端构建7.11s成功；共振+原资金显示两份E2E共19 passed / 18.5s，含榜单双模式/CSV/个股详情/窄屏/三钟及旧信号冻结语义。独立产物outputs/repair_validation_20260914_round5/parent-fund-ui；仍是原5173页面业务API fixture、WS关闭，非生产业务验收。仅既有chunk/NO_COLOR警告。
- 未部署后端、不提升旧缓存版本字段、不改旧数据。

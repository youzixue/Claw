# 模拟盘经济损益/费用展示修复（2026-09-14）

状态：**源码与隔离验证完成，未部署，不能据此宣称生产已修复。**

## 1. 范围与不变量

仅改只读 accounting 展示 helper、paper API 账户/持仓/交易展示返回与挑战者返回装饰、paper 页面损益字段及测试。保留历史 ORM 金额、订单/成交金额、账户初始/现金金额、已有风险指标与执行返回字段；未改预算、止损、门禁、scheduler/main/models/migrations/trading service/public frontend API/style。没有调用生产业务 GET、没有下单、没有启动新服务或部署。旧复盘目录未写入。

现有 `/paper/account`、`/paper/positions` 等 GET 原有的 get-or-create/refresh 写行为**不在此次重构范围**；新增 loader 本身只有 account-id 限定的 SELECT/no_autoflush。隔离 API 测试替换原 refresh/get-create，并断言新增展示无 DML；不能把这个断言宣传为整个历史 GET 已只读。

## 2. 新增经济口径（旧字段兼容保留）

`backend/app/paper/accounting.py`：
- 普通值输入的纯重放 `accounting_snapshot`，Decimal、账户范围强校验、按股票/时间/id维护库存和完整轮次。
- **gross浮盈** = (持仓现价 − 存储买入VWAP) × 余股；旧 `profit_loss/profit_pct` 不变。
- **净浮盈** = gross − 余仓实际已付买费；**不预扣未来卖出佣金/税**。净浮盈率分母为余仓成本加余仓买费。
- **经济净已实现**：每次卖出所得减卖费、加权分摊实际买入本金及已付买费；不是按卖出日价格涨跌计算。
- **持仓生命周期净损益**：该轮之前部分卖出的经济净实现 + 当前净浮盈 + 已单列的存储VWAP舍入桥接；完整平仓轮次直接为全周期买卖现金流净额。
- **今日卖出净实现 / 今日账本已实现** 单列，二者均含跨日持有期收益，绝不当作今日净值变动。
- **今日净值变动**复用现有 `_account_return_breakdown`：相对**上一交易日真实NAV**，持仓行情日期/未来时间校验，非交易日/盘前/缺前值/陈旧行情返回未知，不以本金或更早NAV冒充。
- **完整轮次绩效**与全量账户现金事实分开：forced_probe/excluded_from_performance任一腿命中即排除该轮绩效，真实现金仍保留以对账；提供轮次版本集合、计数、净PnL、净胜率。此处是账户成立以来跨版本背景，**不是当前实验协议样本**。
- 主账户、6挑战者、关闭legacy账户由account_id独立计算；不合并现金/本金/收益率。仅证据路线不伪造账户数据。

新增 `accounting` 返回至 `/account`、`/positions`、`/trades`（卖出行净实现）、`/challengers/comparison` 两侧。旧金额、PnL字段未被覆盖；页面同时显示gross/net/余仓买费/生命周期、账本与经济差额，交易费用分佣金/税/合计。旧账本胜率明确历史标签，避免误称净绩效。买入日志行的毛浮盈可能重复引用同股余仓，页面明确不可逐行相加。

## 3. legacy与对账：只展示桥接，不改旧数据

逐笔计算净经济值 − 原realized_pnl。**仅当卖出日期早于2026-09-07且差额与该笔真实分摊买费负值一致（分位容差）**，归入 `legacy_entry_fee_adjustment`。其他差额不武断解释为旧费用，列 `unexplained_realized_adjustment`；存储买入VWAP舍入另列，现金和资产残差另列。未知买入链/负手续费/非有限金额/库存不匹配等 fail closed，返回null/问题，不以0伪造。

对账身份：
- 实际现金 − 初始资金 − 历史净交易现金流 = cash residual。
- 存储总资产 − 实际现金 − 余仓市值 = asset residual。
- 经济累计PnL = 经济净已实现 + 净浮盈 + VWAP舍入桥接。
- 已付费用已在现金/收益中，展示后不可再次从净值扣减。

9/14冻结事实复核：
| 项目 | 数值 |
|---|---:|
| 账户 | 13（6主策略+6隔离挑战者+1关闭legacy） |
| 历史成交 / 今日成交 / 开放仓 | 110 / 25 / 9 |
| 全账户开放仓gross | 1343.99 |
| 余仓已付买费 | 45.00 |
| 全账户开放仓净浮盈 | 1298.99 |
| 今日卖出账本/经济净已实现 | -3817.16 / -3817.16 |
| 核验旧买费只读调整合计 | -141.97 |
| 13账户现金/资产/其他未解释差额 | 全部0 |

该合计仅作审计加总，不作为合并策略绩效。父审精确prev_close库存桥接的今日MTM **-781.18** 与 -3817.16 不是同一口径；本修复**没有**把该日期数字硬编码到页面，也没有把6位NAV估算宣传为那项精确桥接。

旧买费调整按id：1=-103.72、2=-4.03、3=-11.34、6=-2.20、7=-1.26、8=-5.11、9=-11.33、10=-2.12、11=-0.86，其余0。13账户历史金额均不变。

## 4. 验证

统一后端环境：
```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false \
/usr/local/bin/python3.11 -B -m pytest \
 tests/test_paper_accounting.py tests/test_paper_nav_reporting.py \
 tests/test_paper_numeric_boundaries.py tests/test_paper_api.py -q -p no:cacheprovider
```
结果 **352 passed / 40.68s**（一个既有python_multipart弃用警告）。

新增 `test_paper_accounting.py` 覆盖费用不重复扣除、旧费桥接/未知差额、部分退出再加仓、完整轮次/强制探针排除、跨账户/未来/缺链/异常值拒绝、VWAP舍入、现金残差、只有SELECT且不flush待写外账户、API旧字段不变、3只真实浮亏仓价格。

可选冻结测试 `test_paper_accounting_frozen.py`：
```sh
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false \
PAPER_ACCOUNTING_EVIDENCE=/Users/youzix/WorkBuddy/Claw/outputs/postmarket_review_20260914_1717/evidence.sqlite \
/usr/local/bin/python3.11 -B -m pytest tests/test_paper_accounting_frozen.py -q -p no:cacheprovider
```
**1 passed / 1.47s**。连接 `mode=ro` + `PRAGMA query_only=ON`；断言前后SHA256均：
`68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05`。没有环境变量时跳过，防止普通回归依赖外部冻结大库。

前端：`npm run build`成功（已有大chunk警告）。
```sh
npx playwright test e2e/paper.pnl-display.spec.cjs e2e/paper.accounting-repair.spec.cjs \
 --reporter=line --workers=1 --output=test-results/paper-accounting-regression
```
**11 passed / 17.7s**。使用既有5173，全部 `/api/v1/**`由fixture fulfil，全部websocket关闭；未放行真实API。1440/390视口、未知值不回填gross/零、主/挑战者分账、新旧字段兼容、页面无异常/无写请求。已人工查看390截图，损益分项换行正常且无横向页面溢出。截图在 `frontend/test-results/paper-accounting-regression/`（隔离样本，不是生产截图）。

## 5. 剩余具体边界 / 协调建议

1. 今日MTM仍受历史NAV六位精度影响，可能有分位误差；无昨NAV即未知。若需和复盘完全一致，后续另加精确期初现金+昨收持仓快照契约，不能靠补写旧NAV修平。
2. gross/net浮盈使用持仓存储现价，明确不是新鲜报价保证；只有今日NAV模块校验报价日/时点。本次不补行情连续性、quote gap或上游调度。
3. 当前执行 `paper_sell`已有cycle全部buy量分摊commission，与先减仓再加仓时剩余库存经济成本不必一致。此修复将差额公开，不改成交记录/资金；9/14冻结样本该项差额均0。后续若要改未来成交费用入账，需独立批准并覆盖所有部分卖出/加仓/最终清仓守恒，避免误动历史。
4. 历史成交日志时间可为逻辑轮时间；本helper按现有time/id排序，不能证明物理提交或撮合时钟。002860旧fill时间早于order.created的审计缺口仍需真实submitted/matched/received时钟字段协调，未触碰trading service/models。
5. 全量账户经济PnL必须保留被排除交易现金事实。当前协议比较仍以原experiment_report的完整有效轮次为准；不使用本helper跨版本净胜率调整策略预算或自动晋级。
6. 不完整链条返回未知，不推定现金差额一定是手续费；未来若有存取款/分红/拆股而无对应账本事件，会明确未对账，不能静默计入收益。
7. 未重启后端/未部署；既有GET写刷新行为仍在。生产验收应在父会话统一安排，不应通过本次隔离测试越权触发真实接口。

## 6. 文件

- `backend/app/paper/accounting.py`（新增纯经济helper/只读loader）
- `backend/app/api/v1/paper.py`（仅展示helper及四个返回段）
- `frontend/src/views/paper/Index.vue`（损益/费用标签、分项与未知值）
- `backend/tests/test_paper_accounting.py`
- `backend/tests/test_paper_accounting_frozen.py`
- `frontend/e2e/paper.accounting-repair.spec.cjs`
- `frontend/e2e/paper.pnl-display.spec.cjs`（同步旧账本标签断言、加WS隔离）
- 本报告。

最终交易记录说明文字补齐后再次build成功，新经济口径e2e **3 passed / 4.0s**；上述11项为此前同一数值逻辑回归。git diff --check通过。所有本任务后台job均已收取且完成，无遗留运行job。

工作树已有大量并行改动/未跟踪文件，本次未将整文件diff误归为己方修改，也未执行git reset/checkout/commit。

## 7. 父审 residual 边界补丁（源码已冻结）

仅追加现金/资产残差的明确不平衡原因及主/挑战者可见展示。null显示--，零显示0.00；unreconciled而issues空的旧响应也不再笼统归因为缺数据。提示“仅展示、不重复扣净值”，未更改残差公式、PnL/NAV金额、原API或交易链。

单独验收：`tests/test_paper_accounting.py` **18 passed / 2.93s**（参数化API覆盖无差额、单现金、单资产、两者不平衡，断言原因与净值不重复扣减）。`npm run build` **成功 / 7.06s**；`paper.accounting-repair.spec.cjs` **4 passed / 3.6s**（全部API/ws隔离；非零残差、缺失null、主/挑战者数值不变、1440/390）。首次源码edit被read前置检查阻止，旧源码红测3例确认缺原因；旧UI任务已停止，以上均为补丁落地后的重跑。相关job113/114/115/116均已收取，无运行任务。

冻结SHA256：
- accounting.py：`4b4e18f5c8cef0117190197eb329c5838bb7415331eb6a64da420b6fb7ce7bf7`
- paper/Index.vue：`46092d46de177d38ccf5c796774b89dba27710be9a4f21d32a0daf6c3fbcf9d6`
- test_paper_accounting.py：`9d2fda26ca9d7140c90cdeed08e7ba6588114249bd45d2630dbcffcc3a2421ad`
- paper.accounting-repair.spec.cjs：`c0f7dac040b8bb6ce618ad870c2258cc4ea49f5c3b035509e1fdff7b3e8a01cd`

停止扩展；精确期初快照、物理时钟、未来卖费入账仍交父后续协调，未部署。


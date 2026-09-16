# 正式预测缺批 / 逐路线只读诊断修复（2026-09-14）

## 范围与结论

本次仅新增**读取持久化正式批次的解释合同**，不改变正式预测生成、排序、概率、Champion、路线门禁或交易消费。未部署、未启动服务、未下单、未补造旧run，也没有调用会写数据的候选GET。

已阅读AGENTS、promotion-api、父修复进度和相关ledger/API实现。导航确认：

- `ledger.py`已有独立ScheduleBatch身份、追加式generation_pending开始屏障和不可变阻断尝试；`persistence.py/versioning.py`已有严格概率空批拒绝，本次没有重复实现。
- 原`_build_prediction_snapshot_health`主要统计兼容record的日期/赛道/模型/竞价健康，没有按完整正式时间窗对照不可变run尝试。
- 原进程内scheduler健康不能证明进程缺席期间“没有尝试”的原因。新增报告仅说**未读到持久化正式尝试**，不宣称进程缺席、调度必然未运行或当时市场无机会。

## 实际改动与所有权

1. `backend/app/promotion/batch_diagnostics.py`
   - 新增纯函数`build_batch_diagnostics`及SELECT-only异步加载器`load_batch_diagnostics`。
   - 读取`TradeCalendarModel`、`PromotionPredictionRun`与分组后的`PromotionPredictionSnapshot`，不读取兼容record来冒充完整尝试史。
   - 使用`db.no_autoflush`，不调用create_all、ensure storage、flush、commit、rollback、数据采集或推理。
   - 窗口、盘后context和必需路线由既有调用方常量注入，未复制一套调度时间表或同义业务引擎。
2. `backend/app/api/v1/promotion.py`
   - 仅追加专用无缓存GET `/api/v1/promotion/batch-health?trade_date=2026-09-14`。
   - 不调用`promotion_candidates`、不碰其缓存/持久化；原候选health字段与消费者逻辑未改。
   - 默认日期为检查墙钟当日，显式日期按登记日历判断；已知周末/官方休市覆盖为非预期交易会话，不调用日历补源网络。
3. `backend/tests/test_promotion_batch_diagnostics.py`
   - 原48个参数展开测试，父复核另加24例（共72例），包含纯诊断、隔离SQLite和ASGI只读路由验证。
4. 本报告。

未修改scheduler.py/main.py/models/migrations/前端、父进度文档或旧复盘证据目录。API采用读取后局部精确替换，保留其他并行改动。

## 父复核边界补强

- 当前`PromotionPredictionSnapshot`的`prediction_trade_date`与`candidate_route`均为`nullable=False`，但路线默认值是空字符串；不能以当前ORM约束证明旧数据库投影无NULL或空路线。
- 缺失/空白/非字符串路线统一输出`route=null`、门禁unknown并标记`missing_or_invalid_candidate_route`；未知非空路线保留原名、标记`unknown_candidate_route`。两者均使批次invalid，不从空键、未知键或全局gate推导通过，数量仍保留在分母中。
- 目标日期仅接受date或严格ISO日期字符串；异常类型、无效日期明确invalid，不参与混合排序/未来日期比较。ORM解码已损坏日期失败则返回`evidence_unavailable`，不是完整空日。
- 追加20个纯函数边界案例及4个隔离SQLite只读加载案例；未改变`_attempt`时钟、generation_pending、最新批次选择或禁止catchup契约。
- 最终六文件回归（bash-110，与下述相同隔离环境/命令）：**527 passed，1条既有python_multipart弃用警告，44.10s，exit 0**。后台任务已收取；未部署或修补历史批次。

## 新接口合同

schema为`promotion_formal_batch_diagnostics_v1`，scope为`persisted_formal_attempts_read_only`。

### 窗口与批次状态

|状态|含义|
|---|---|
|not_due|尚未进入原生成许可窗口，不算漏批|
|awaiting_persisted_attempt|窗口仍开，尚未读到持久化尝试；不武断判生成失败|
|missing_persisted_attempt|窗口已过期，仍无匹配正式尝试；原因unknown|
|generation_pending_unresolved|最新正式持久化尝试是开始屏障，尚无后续有效完成；可能仍在跑，不推定超时根因|
|persisted_blocked|最新正式尝试为非completed阻断；保留原status/persistence_status|
|completed_empty_unproven|run称completed但零候选；不是全市场真实零机会证明|
|completed_route_blocked|run完成但至少一路明确blocked；保留整批与逐路线的不同结论|
|completed_batch_gate_not_passed|整批未通过；逐路线原字段仍独立保留，不能据此篡改实际路线消费者合同|
|completed_route_gate_unknown|必需路线未有合格门禁证据；不以整批ok补成路线ok|
|invalid_persisted_attempt|模型身份/时钟/目标日期/数量合同存在问题|
|calendar_unknown / not_expected_closed_session|日历未知/明确休市，不伪造应有交易批次|
|evidence_unavailable|缺表、读失败或超过500尝试有界预算；不把残缺查询当成完整空日|

- 原生成器用(hour, minute)包含结束分钟，故15:10窗口的16:30:59仍在窗口，16:31才expired；测试覆盖该边界。
- `automatic_catchup_allowed=false`与`fallback_to_older_run=false`明确该诊断没有补跑或旧榜回退授权。原调度若要在仍开放窗口重试，继续走原日历/时钟/屏障合同，不能依据该报告绕过闸门。
- 每个context保留全部有界尝试和最后一条；**先按as_of_at/id选最新，再验状态、身份和时钟**。较新失败、错误模型或缺赛道不得借旧成功掩盖。
- 无法从GET/scheduler任务正常返回推导正式业务完成。

### 四类身份 / 时钟不能混为一谈

- **会话/context**：匹配正式source且reference_trade_date或as_of会话属于请求日，不以created_at当天就判今天有正式批次。未知context的原id另外报告。
- **实际记录钟**：同时返回as_of_at、created_at、completed_at。检验本地无时区时钟顺序、原窗口及检查cutoff；completion clock不是物理commit/浏览器接收回执，历史PIT认证固定false。
- **目标日期**：保留metadata声明的trade_date_by_target和snapshot实际prediction_trade_date分布；不把老二板candidate anchor等于今日信号日。盘后旧目标日期不能填今日收盘窗口。
- **模型身份**：四元组model_version/feature_version/data_version/runtime_mode逐一保留并对照调用方当前配置；配置身份不是历史部署证明，混合/旧版本不自动冒充当前。较新的其他身份不因筛选被隐去以回退旧成功。

### 逐路线解释

逐路线输出原gate_passed、passed/blocked/unknown、blocking_datasets/issue_types、候选/排名/actionable原计数、candidate_presence和execution_eligibility=not_evaluated。

- 全局gate=true不覆盖route=false。
- 全局gate=false也不抹掉某route=true事实；本报告不是重写B/C/D消费者规则。
- 没有route_gates或不在既有必需路线合同内的路线显示unknown，不能从整批标志猜测。
- 没有候选只说明该run中absent_unproven；没有snapshot的目标赛道也不解释成完整零机会。
- counts与snapshot聚合不一致、未知target、未来target等显式标记，坏数据不通过删除分母变好。
- 所有candidate_or_execution_authorization、process_absence_proven、historical_point_in_time_certified均false。

## 真实只读核验

2026-09-14 **18:57:49.090953**，直接调用新加载器读取生产`backend/claw.db`，SQLite URI `mode=ro`，会话`PRAGMA query_only=ON`并断言为1；没有调用业务HTTP GET或storage初始化。

结果：

|context|持久化尝试数|最新run|诊断|
|---|---:|---:|---|
|0925|1|75|completed_route_blocked|
|0935|2|77|completed_route_blocked|
|1000|3|80|completed_route_blocked|
|1030|3|83|completed_route_blocked，三条治理路线均blocked|
|1305|2|85|completed_route_blocked，竞价blocked，主线/二板passed|
|1510|0|无|missing_persisted_attempt，窗口expired|
|2000|0|无|not_due|

共11个真实正式run；1510仍无持久化正式尝试，不借1305成功充当1510。不从此结果判断进程在15:10是否存在、生成在哪一步失败，亦不补跑窗口已过的15:10。18:57的读取结果不是部署证明。

第一次只读验证命令将应用全局DATABASE_URL设为:memory:以隔离未用全局连接，但现有session.py的pool_size/max_overflow参数不支持StaticPool，导入前失败(exit1)，未连接业务库。确认源码后改为全局与本次显式连接均使用同一`mode=ro&uri=true`文件URI，且实际会话query_only，核验成功(exit0)。未为验证更改session.py。

## 测试与自检

统一命令前缀：

```bash
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false \
/usr/local/bin/python3.11 -B -m pytest -q -p no:cacheprovider
```

- 初轮：39 passed、4 errors。错误为新增async fixture误用pytest.fixture，与pytest-asyncio strict模式不兼容；已改为pytest_asyncio.fixture，不改生产逻辑。
- 修正后，新诊断+原batch_attempts+generation_barrier+probability_contract：**292 passed，1 warning，24.31s**。
- 补模型四元组、盘后目标错日、未知context/target、批门/路线分离等测试后，最终联合：
  `test_promotion_batch_diagnostics.py test_promotion_batch_attempts.py test_promotion_generation_barrier.py test_promotion_probability_contract.py test_promotion_ledger.py test_promotion_api.py`
  **503 passed，1 warning，41.91s，exit0**。
- warning为既有python_multipart弃用提示。
- 隔离数据库路由测试将session设为query_only，跟踪SQL只出现PRAGMA/SELECT；留在session.new的对象未autoflush；缺表时不创建任何表；生成函数/日历网络fallback/storage initializer被替换为一调用就失败，仍通过。
- 没有以测试结果声称新接口已部署、日历身份已前向认证或预测收益改善。
- 本代理后台测试job bash-93/94/97均已收取，最后一个才是扩展503项回归（一次进度消息误写95，实际工具返回97）。

## 父会话集成建议与剩余限制

1. 部署后可从新增batch-health读取持久化窗口诊断，与`/health.pipeline.operational_health`当前进程观察并列；**不要把两者合并成“全天调度完整”布尔值**。
2. 如需调度观察集成，由父会话在原scheduler注册/健康投影中引用此只读结果，不在15秒内存观察循环无条件增加重型DB查询；采用既有有限预算或人工请求，且标记checked_at。
3. 源码已有开始屏障只能覆盖日历确认且屏障事务成功之后。进程没运行、日历失败、屏障提交失败仍只能诊断缺批，根因需持久化运维/进程证据，不能靠补假run修复。
4. 原候选GET仍保留原业务行为，本次专用只读endpoint未接前端；不声称用户页面已有新诊断。新报告不改变旧榜显示或订单规则。
5. 500尝试上限触发时整体unavailable，需运维进一步只读审计；不能静默使用截断后的有利批次。
6. 本诊断读取当前可见存储并检查记录钟；没有历史commit receipt、正式证券身份或供应商材料的时点认证，不补造历史PIT。部署及下一交易日正常/失败前向采证仍待父任务验收。

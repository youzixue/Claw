# 新闻实体/角色归因修复（2026-09-14，未部署）

## 范围与安全边界

已先读 AGENTS.md、docs/promotion-api.md、现有新闻处理/PIT代码及对应测试。本任务只修改：

- backend/app/news/engine.py
- backend/app/news/nlp/processor.py
- backend/app/news/catalyst.py
- 新增 backend/app/news/roles.py（纯确定性 helper）
- 新增 backend/tests/test_news_role_repair.py
- 本报告

不修改 scheduler.py、main.py、模型、迁移、前端、交易/风控配置；不改评分公式、权重或 Champion；不下单、不部署、不重启。没有改写旧 outputs/postmarket_review_20260914_1717，也未回填生产新闻/预测。

生产库仅使用 SQLite URI mode=ro + PRAGMA query_only=ON 查询原文；pytest 全部使用既有测试隔离方式，新增集成测试为内存 SQLite + stub NLP。测试中的时钟是隔离模拟，不代表这些原文在模拟时间已经真实可用。

## 只读核对到的原始问题

| ID | 原文证据 | 修复口径 |
|---|---|---|
| 30314 | 标题“CRO概念表现活跃 万邦医药涨超12%”；正文含万邦医药，原 related_codes 不含301520 | 当前精确唯一名称表确定性覆盖标题/正文；原始页面缓存及新分析 related_codes 补入301520。但属于涨后报道，不转成事前硬催化 |
| 29476 | “数码视讯：拟转让博汇科技4.10%股份 交易对价6698.56万元”；正文仅数码视讯显式带300079.SZ | 数码视讯300079为 transaction_subject，博汇科技688004为 transaction_target。另一实体的显式代码不再全篇否定博汇名称映射；双方方向均 unconfirmed |
| 30299 | 浙江太乙圣莲拟出资30亿元换取合众新能源约70.62%股权；太乙圣莲为山子高科董事长叶骥的关联主体 | 上市公司仅属关联背景，不能把拟议投资/重整解释为上市公司直接完成收购。只认原文完整“山子高科”，不凭“山子”短称硬映射 |
| 29361 | 中新赛克AI应用营业收入不超过2026年上半年营业收入2%，尚处商业化起步阶段 | 有限收入暴露/尚未确认，不能包装成确定订单；没有硬编码2%收益或评分阈值 |
| 30138 | 超声电子/金安国纪认证为不实信息、情况不属实、无产品供货 | 原文否认优先于模型乐观摘要，research_only，阻断正向催化 |

这些只是原文及旧页面数据的事实核对，不是市场因果证明。测试使用以上原文及独立虚构股票边界，而非为几个代码设专属规则。

## 实现

### 1. 实体覆盖与角色分离

保留现有 exact_unique_stock_name_v1 校验：六位代码、长度/弱名称限制、唯一名称、长名称冲突保护。无模型代码、裸数字、模糊简称或板块扩散认证。

原先“文章存在任意六位数字，则所有名称的代码都必须在全文数字集合内”的规则会遗漏只写名称的交易标的。现改为只拒绝名称紧邻的矛盾代码；例如“万邦医药（证券代码：688004）”仍不认证，而另一公司的300079.SZ及报道编号不会删掉万邦医药标题实体。

新增 news_roles_v1：
- candidate_codes 是原文已验证提及，不是 positive beneficiaries。
- positive_beneficiary_codes 固定空列表；本层不认证经济受益。
- 转让的卖方/标的仅在狭窄标题语法成立时标角色；不成立就保留 mentioned_entity，不猜方向。
- 多个已验证实体但方向未能分别证明时，整篇降级研究。
- 角色证据保存原文片段和 title + 换行 + content 上的偏移。
- 关联主体、计划/意向、否认、收入暴露限定、涨后报道等全部使用通用语言模式，不包含特定股票代码。
- scope=legacy_eligibility_unmodified 仅表示未命中新安全否决，绝不表示已证正向受益。

### 2. 防止模型摘要/事件冒充事实

processor 和存储边界均执行 guard_news_analysis：
- 被否决文章标记 research_only。
- 正面 sentiment 降为 neutral；已有负面分析不在页面存储时反转为正面。
- 面向既有页面的 summary/events 使用真实标题/正文摘录和 research_context，不继续展示“已完成收购”“确定订单”的模型改写。
- 原模型摘要/事件/情感只保留在新分析 JSON 的 model_hypotheses 以供审计，不作为已证事实。
- related_codes 维持兼容候选字段，可含来源/模型候选；真正用于 PIT 的代码仅来自精确验证。role_evidence.candidate_codes 才是已验证提及集合。
- 被否决文章的 related_sectors 清空，避免新闻方向经板块消费旁路传播。

### 3. 不可变版本与无前视

news_pit_v1 外层协议及模型不变：
- 新接收内容仍先冻结 NewsContentVersion 原文及首次接收/内容可用时点。
- 新分析在观察型内容上重新做当前精确实体验证，并把结果放到新的 NewsAnalysisVersion.result_json.role_evidence 中。
- 不修改任何旧内容/分析行。旧内容漏映射后的重新分析只能在该新分析真实 available_at 后看到新代码；更早 cutoff 仍看到旧映射。
- 首次接收时间 first_received_at、content_available_at 不因重分析刷新；相同连续成功结果继续去重，不制造新的首次可用时点。
- 角色新证据的首次可用以所在分析版本 available_at 为界，不拿原文 publish_time 冒充。
- load_news_evidence_as_of 只用 cutoff 内的原文及分析；不查当前 StockSpot 或 FinanceNews 做历史映射。
- 对旧分析，读取时可仅凭其当时已可见的原文做保守安全否决。这是当前消费者的防误用政策，不是历史证据回填：不增加新正面事实、不改旧分析字节、不改旧预测。旧历史结果不会声称已执行过新版角色规则。
- legacy_unknown、未来/错误时钟、不可用或失败分析的原有闸门不放宽。

### 4. 催化消费

classify_news_event 对标题中的研究型语义不再标硬事件；score_news_catalyst 与 load_direct_stock_catalyst_map 另检查完整原文和 research_only，避免正文否认被标题/模型乐观结论覆盖。即使调用者传 min_score=0，角色否决也不能进入正向催化 map。

共享 PIT 视图新增 role_evidence、research_only、candidate_codes、positive_beneficiary_codes，同时保留原字段。被否决视图中方向/板块中性化。这可能减少消息催化的信号、候选召回或确认计数，但不调整评分参数，不意味着收益/回撤已改善，也不影响既有独立交易风控闸门。

## 验证

最终命令（backend/）：

```bash
PYTHONDONTWRITEBYTECODE=1 QUOTE_ROUND_ARCHIVE_ENABLED=false \
/usr/local/bin/python3.11 -B -m pytest -p no:cacheprovider \
  tests/test_news_role_repair.py \
  tests/test_news_evidence_versions.py \
  tests/test_news_ai_pipeline.py \
  tests/test_news_catalyst.py \
  tests/test_promotion_news_evidence.py \
  tests/test_news_api_logic.py \
  tests/test_news_cleaning_status.py \
  tests/test_news_fetch_latency.py -q
```

结果：**119 passed，1 warning，3.34s，exit code 0**。warning 为已有 starlette/python_multipart 弃用提示。此前基础回归79 passed，扩展中间版本115 passed；以上119为最终测试结果。

新增覆盖：
- 五组真实原文：遗漏实体、交易主体/标的、关联主体、收入上限、否认认证。
- 不依赖具体代码的提议/否认/收入/价格报道、空输入、正常单主体路径。
- 标题实体不被其他实体代码或编号删掉；紧邻矛盾代码、弱简称、重名不认证。
- processor 独立阻止AI否认内容变确定订单。
- 新映射只能在新分析 available_at 后可见；旧JSON/hash/内容行不变；重复同结果不刷新时点。
- 旧乐观AI分析在原文否认时消费降级，但原始分析字节保持不变。
- 原始缓存未运行AI也覆盖标题实体。
- 既有未来/失败/更正内容/不可变约束/PIT仅读/晋级消费测试继续通过。

未运行全仓库测试，未进行部署/真实抓取/线上页面验收；本报告不声称生产条目已被重新分析。

## 生产状态补充（父会话只读核验，本子任务未重复核验）

- 父会话报告：liveDB 的 alembic_version 仍为027；news_content_version、news_analysis_version、factor_evaluation_run 表已存在，但 sqlite_master 未发现新闻版本表的 append-only triggers。
- 因此，以上隔离测试通过不能证明当前生产库已具备数据库级不可变保护。ORM after_create 不会为已存在的表补触发器；029升级的补齐及部署前schema预检由父会话负责，本子任务不修改模型/迁移、不执行升级。
- 父会话报告：8000服务 PID57866 仍为15:34启动，尚未加载本轮代码。本任务交付仅为待部署源码和测试，不宣称生产修复已生效。

## 限制和后续

1. 这是保守安全门，不是完整中文事件语义系统。较复杂卖方/受让方、代词、跨句关系与未覆盖措辞仍需原文人工核验；可能漏拦，也可能因同篇否认/多主体而过度拦截有效消息。不能把“没有命中规则”当作订单证明。
2. 角色证据中的交易主体/标的只说明语法角色，不说明交易已经完成，也不证明哪个上市公司经济上受益。关联背景标签表示“不认证为直接收购方”，不是断言上市公司法律上绝不参与。
3. 现有当前股票名称表可能缺公司或改名；没有历史简称表时，不用弱名字补确定映射。离线无数据库的 processor 不自行查询股票表；标题确定性补全在引擎数据库路径完成。
4. 候选仍按既有主板/创业板/科创板/风控规则处理；301520和688004被实体识别不等于获得交易资格。
5. 没有修改历史输出、生产DB、旧预测或 Champion。若之后人工批准运行新分析，只能追加新版本并使用真实完成/可用钟，不得倒签至9/14更早批次。

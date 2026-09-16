# 指数历史投影保护（2026-09-14）

## 已确认问题
新调度器的盘外/启动指数窗口刷新会调用reconcile_index_history：
- 缺失历史日期被写入StockDaily；
- close无效的原行被原位替换，包括其它OHLC/派生字段；
- 本次来源响应虽然是真实数据，但其实际观察发生在今天，不能据此改写或解释过去的市场/决策证据。

## 源码修复
- `backend/app/data/index_history.py`：保留reconcile函数名兼容，但改成只读对账，无write开关。日历在no_autoflush上下文中读取，行情按列SELECT且autoflush=False，避开调用方脏ORM值。不新增、不更新、不flush历史行情。
- 对账结果明确mode=audit_only_no_historical_projection、historical_projection_changed=false；inserted/repaired恒为0。缺失/坏行仅列candidate日期，missing_after仍是原缺口，不能将“有候选值”误称投影已修复。
- `backend/app/data/index_history_window.py`：完整来源仍按实际观察时点追加原来的65会话冻结输入与哈希，保存projection_audit，而非自动修价；来源complete和投影complete分开。已有合格窗口重启仍不重复采集。
- 限定部署复核另发现60秒指数采集实时失败时回退历史日线也能原位写旧日。父修复scheduler该局部：非today明确跳过，复用原当前有效指数筛选在写前过滤；即使0/3也记质量失败。通用StockDaily upsert在写入瞬间再检查实际当天，防网络跨午夜后写旧日；新增7例过去/未来/未知/字符串拒绝及stale/mixed/current实际入口。
- 来源可用性、代码/来源身份、65会话、收盘时点、未来拒绝和现有收益/风控模型未放宽。缺失历史不自动复原；新冻结输入仅适用于可知时间之后的既有消费者。
- 这不是迁移新表，也不宣称DataSourceHealth整张通用健康表获得了数据库级不可变约束；本入口只追加证据，过去health/历史行情内容不修改。

## 测试
- 调整旧“应补缺/应覆盖坏行”测试，保留真实日历、未来/休市过滤、原有效值保留等断言，新增只读SELECT、调用者dirty/new不flush、缺失/无效/有效投影完整字段不变及实际时点可见性。
- 4文件（指数历史/窗口、资金窗口、调度K线）：**101 passed / 18.92s**，bash-140 exit0。
- 7文件（指数历史/窗口、证据迁移、K线观察、正式恢复、模拟会计、市场风格）：**117 passed / 19.42s**，bash-141 exit0。
- 均隔离临时库和来源，无真实API采集或下单；仅既有python_multipart警告。
- 后续包含60秒fallback/写边界保护、指数历史/窗口、情绪数值、调度K线、部署审计工具的6文件交叉：**200 passed / 22.43s**，bash-148 exit0。只读工具新增比较器后4项单测通过0.69s（147）。均不与重叠集合相加。
- 未与上一阶段955项相加；以上测试不证明线上已部署或收益改善。部署事实单独记录。

## 新增部署审计工具
`backend/scripts/audit_deployment_evidence.py` 不导入应用/settings，不初始化数据库；sqlite URI mode=ro + query_only + 单个只读事务，对全部表计数及27个关键历史/交易表逐行摘要（Python3.11 tuple repr协议）输出哈希，不输出原始新闻或账户内容。输出文件必须新建且不得覆盖db/WAL/SHM。摘要用于副本演练和生产迁移前后对照，不替代一致性备份或完整性检查。

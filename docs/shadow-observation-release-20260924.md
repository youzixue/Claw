# 9/24复盘修正：观察v2已发布

## 当前结论
2026-09-24 23:37:16（上海时间）已完成原服务受控发布：原后端8000由PID35134切换为49797，实际加载`research:strategy_candidate_experiment_20260924_v2`及`compact_json_gzip_level1_v1`。不是只写代码，也没有启动替代服务。此前未发布/拒绝记录保留，本文件补充最终状态，不覆盖旧签认证据。

**这次上线的是影子对照和工程修复，不是把实验算法替换为12户正式买点。** 原12户继续各自独立运行；每户初始资金5万元、执行版本、买点阈值、仓位、风控、正式通知身份不变。共享新增仍关闭，旧账/旧退出不迁移、不补单。

## 修了什么、为什么
|范围|改动|限制|
|---|---|---|
|研究确认有效性|绑定原账户/版本和原执行TTL；缓存也重新检查当时有效期，过期另记状态；缺证保持unknown|不把旧确认刷新成当前可买；历史冻结数据缺新合同不能反补|
|C2观察v2|原确认有效、原门禁通过后，要求不同源连续样本持续至少原研究30秒；VWAP逐段不下降，价格逐段不下降且首尾确有上涨、相对强度不负、当前价不低于VWAP|平VWAP可以，平/跌价不能；不是严格正VWAP斜率算法，也不是低位预言。只观察，不下单、不发正式提醒|
|其余路线|保留原版和v1；F2缺证与主动过滤分开|没有证据不强改每条路线；A2提前观察未显示优势，B2样本太小，E2/主路线等无足够可比确认|
|研究存储|新记录紧凑JSON+无损gzip；有压缩/解压/文件/队列预算与5GiB磁盘保留；异常标记缺口，不伪装完整|不删除旧失败记录，不复制整库；有界读取不是完整日分母|
|C3通知读取|按stage先投影，再400个ID一批读全部匹配payload，最后统一运行原状态机|不裁剪sent/失败历史，不改TTL/冷却/小时上限/发送条件；依赖既有日志append-only约定|
|原比较页|并列原版、实验v1/v2、原确认有效性及早观察|研究命中不等于正式可买；不新增共享页面|

9/24完整冻结对照见`outputs/shadow_comparison_20260924/REPORT.md`。C2原确认20/67参考收盘为正，v1为17/58，平均参考收益反而更差（−0.694%→−0.945%），因此保留失败v1、增加v2作可证伪对照，不晋级生产。上述为首次确认参考价到收盘，不含成交可得性、费用或T+1，不能称实盘净胜率。

## 自测和独审
- 最终736文件源码冻结；19个后端文件765项通过，源码稳定，隔离网络/仓库运行库；原611项证据保留，不与765累加。
- 前端9项模型、7项API拦截浏览器测试及构建通过；不是自然市场端到端测试。
- 存储、研究核心、发布门、C3查询/生命周期独审分别有证据。最终晚窗32项fake保护测试通过（含父19项），原C3函数9项fake读分支结果按原hash复用，不冒称重新执行或真实排空。
- C3同一只读快照122行逐字段相等：旧查询39.5ms，新完整投影+payload约3.8ms。单次样本，不代表盘中SLA或收益改善。

## 实际发布与保护
- 签认193项证据；新窗口23:35–23:55，核实当天23:30指数任务于23:30:01.244985完成。旧终态不能代替本次完成；保留8分钟签认余量、5分钟执行余量、180秒调度距离及两次fresh证明。
- 两轮双采样保留C3真实running/idle状态，依据本轮开始前已终态旧基线、当前完整队列和当日无新确认，证明限定读分支；不是把running改idle，也不是已修复通用shutdown drain。
- 原launchd服务一次SIGTERM；新进程启动初期2次URLError如实保留，随后健康/调度/观察worker通过。
- baseline/pre/post共14组保护账本，真实文件行数/字节/hash一致；12户版本与激活、每户初始5万元、现金核对不变，挂单为空；没有试发买点、补单或迁移持仓。
- 原8000只读C2接口与5173页面/模块检查通过。该历史C2请求`limit=1`返回0股票行、`truncated=true`，原因含旧日文件数/字节预算和行上限：**不能据此说当天没扫描，或宣称全日查询验收完成**。完整日对照使用先前冻结分析，不扩大运行接口预算硬读旧14GB。
- 新进程真实gzip启动记录已核验：905字节，解压3232字节，CRC/EOF/文件hash和session一致；仅1条`restart_boundary`，无生产权限，不代表已收到自然行情或全日压缩率。
- 23:40:46再次只读核实PID49797、health=ok、调度及观察worker运行，版本/权限不变；父独立重算193签认hash和42份账本实际字节/行数/hash全部一致。

## 验收边界
已验收：发布/加载、隔离回归、14组账务参数无变化、原地址只读接口、真实新压缩启动记录。发布后独立纯文件复核通过：193签认证据、736源码、42份账本及meta、四份fresh证明、12户及PID/v2身份均一致，见`outputs/shadow_repair_20260924/maintenance_late/post_release_review.md`；独审不冒称现场发送/成交或自然行情验证。

未验收：下一交易日同候选同报价的自然v2样本、完整日资源占用、跨日收益/回撤及召回损失。`natural_forward_accepted=false`。不承诺明天收盘浮盈，也不把盘后发布解释为今天已经改善。

## 主要文件和证据
- `backend/app/paper/intraday_route_research.py`
- `backend/app/paper/candidate_shadow.py`
- `backend/app/push/paper_buy_points.py`
- `frontend/src/views/paper/CandidateShadowPanel.vue`
- `frontend/src/views/paper/candidateShadowModel.js`
- `outputs/shadow_repair_20260924/c3_followup/VERIFICATION.json`
- `outputs/shadow_repair_20260924/maintenance_late/release_result.json`
- `outputs/shadow_repair_20260924/maintenance_late/post_release_smoke.json`
- `outputs/shadow_repair_20260924/maintenance_late/startup_gzip_verified.json`

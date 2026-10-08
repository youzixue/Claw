# 2026-09-28 十二策略修复与TLS：受控发布完成

## 最新结论（22:08:32 +08:00）
**已补审TLS并合并发布，原8000服务正常重启成功。** 22:08:02只向原PID49797发送一次SIGTERM，由原launchd监督者启动新PID14208；没有替代服务、强杀、手工账本写入、旧仓迁移、取消订单或通知补发。原5173前端PID26800保持不变。

### 发布与保护核验
- 新目录 `outputs/september_strategy_release_20260928_tls/` 独立冻结739项源码、配置与依赖，并封存121项发布证据。相对原候选仅新增已授权TLS源码及其专项测试；旧21:40拦截记录未覆盖。
- 21:52基线→发布前→发布后14组SELECT全量EOF/行数/字节/SHA完全相等：账户14、交易244、成交427、订单2452、历史持仓116、卖出核算72、通知66。待单0；22条通知源仍全部已有sent回执，未出现新发送租约；C3当日confirmed仍0。
- 原accounting.py纯函数对全部14账户复算status=ok、issues空、现金/资产/未解释实现差额0，前后逐账户相同。原12独立账户本金各5万元不变，旧仓冻结退出及历史版本不改。
- 执行身份仅E tenbagger：293f7851aab7→a93621337720；E2 challenger_e：332f5aa5dee9→4981dd23cc5f。其余10路不变；共享组合仍禁止新开仓。
- 调度器52项任务、通知worker、候选观察worker均恢复。真实旧stop/new restart边界已核验，观察核心未改、无生产执行权限；旧日内censored记录0。边界和双采样不是全局原子drain或连续市场观察保证。
- 新启动日志确认服务生成 `runtime/tls/claw_ca_bundle_c3fe17c2350705e5f65bf4f23e98f23867d2b754a0a6ca28d7c79658c1fcfc7d.pem`，301489字节。内容与certifi根+携带中间证书完全一致、摘要文件名匹配、SSL与主机名校验开启。只读本地证书验收通过，不冒充外部数据源握手成功。
- 原5173首页/源码模块200，修正文案仍在。此为静态模块核验，不冒充全页面交互测试。

### 测试与过程披露
- 真实部署解释器的25模块回归：1387通过、1冻结fixture缺失跳过、15 subtests通过。739项源全集、配置及运行环境与21:39结果完全相等，因此复用不可变原结果，未重跑、未改写其时间。
- TLS专项18通过；控制器46项、边界35项、TLS精确授权门9项通过；最终独立闭合审查通过。
- TLS初轮18用例通过，但隔离审计拦截urllib3导入时IPv6探测，因此不予放行。v2仅在隔离测试进程导入urllib3期间关闭该可用性探测、随后恢复，仍阻断全部socket事件；新跑18通过、runner退出0、blocked为空。原红结果完整保留，未改生产或测试求绿。两份v2结果JSON语义相同，仅末尾换行不同，分别绑定哈希。
- 22:06一次只读版本查询超时，发生在DB采集和单次marker之前，**没有发信号**。失败现场另存pre_release_failed_http_timeout/及execute_failed_http_timeout.log；确认原PID、fresh readiness、源码和签认仍有效后重做准备，没有放宽超时或消费第二次重启。
- 父曾因glob相对子路径返回空误判子任务无文件，重复只读source gate在独占创建处被拒绝；原成功结果未覆盖。独审历史RED JSON及TLS红证据保留，旧独审RED说明MD未完整归档，不伪称已恢复。详见parent_execution_notes.md和独审归档说明。
- 已结束TLS临时夹具逐项登记并核对无打开句柄后清理；脚本、源指纹、XML、成功/失败日志及结果保留，不复制运行库。

### 交付及自然验收边界
工程发布已完成，证据见 `release_result.json`、`delivery_verification.json`、`tls_postcheck.json`、`shadow_boundaries.json` 和前后冻结账务。原修复文档/manifest中的“未发布”是较早历史状态，未为更新描述篡改原签名材料。

**natural_forward_accepted=false。** TLS恢复真实外部采集、涨停池和合法新快照仍待自然任务验证，不强制回填或伪造历史时点。下一真实交易时段还须按版本检查模式连续确认、模式变化取消、一字/跌停拒绝、真实撮合与旧仓退出；不宣称十二策略已盈利或收益改善成立。原run_review缺模块、价格链/OHLC隔离及其他数据质量问题未因此宣称修好。TLS缓存不是后台自愈，显式运维CA配置仍优先。

---

## 历史拦截状态（21:42 +08:00，以下为发布前记录）
**未发布、未发送SIGTERM、未重启。** 21:40签认在源码一致性检查处停止；原8000 launchd进程仍为PID49797（9/24 23:36:49启动），原前端5173 PID26800未改。没有生成本轮review.json、validation.json或restart_requested.json，没有下单、取消待单、补发通知或手改数据库。

阻断是等待窗口期间新增的两项源码变化，不能自动带入原E/E2发布：
- backend/app/core/tls_trust.py：a392fe69fb960d1bbf1703d8f586bb74d2915105fa7034a907f1b10f1a482ce2 → 39f64d803ef89c60e235e7a994cb308f803a1ec61e197087922bccbc4747082e。
- backend/tests/test_tls_trust_extra_ca_20260917.py：1c37fa3716e34c4041ec2870e2efccaaedc22e4050a56b50709fa6d9dad2fbde → 42565890b6ed5ac70285f1a47b2defa7cf2514ad5963270db157e4caa6785bc3。

已向用户提出一次范围确认：补审TLS并重新冻结后合并发布，或暂缓维持原服务。未回滚/覆盖上述变化，未削弱源检查。原scope和独审不自动授权新组合；继续前必须新冻结、补充专项验证、重新签认，而非改旧manifest把红门变绿。

## 已完成证据
- 20:21只读BEGIN快照14组完整EOF：账户14、交易244、成交427、订单2452、历史持仓116、卖出核算72、待单0、C3当日confirmed0、通知66。
- 原accounting_snapshot纯函数复算冻结JSON：13active+1closed全部status=ok、issues空，现金/资产/库存/未解释实现差额均0；原12个独立账户各初始5万元。估值基于已存持仓价格，不冒充实时重估或策略收益验收。
- 21:32:37–21:32:40两次新短RO通知快照：与基线逐字节相等，22个源全部有最新sent回执，无pending/attempting，C3 confirmed仍0。worker可见状态稳定只是有界无发送证据，不是原子drain或用户已读证明。
- 原拟20:48窗口取消。准确源码规定2000恢复窗口直到21:30（含该分钟），21:32已观察outside_recovery_window、prediction_running=false，并核验包括21:00/21:20及21:30指数任务在内的12项真实APS终态。21:00/21:20终态为business_degraded，不包装成业务成功。
- 仅E/E2执行身份原预期变更：tenbagger末段293f7851aab7→a93621337720；challenger_e 332f5aa5dee9→4981dd23cc5f；其余10路不变。旧仓冻结退出参数和旧成交身份不迁移。但新版本尚未由本轮服务重启激活。
- 控制器46项隔离门测试、独审20项门测试、35项gzip边界测试及4项证据闭合检查通过。一次SIGTERM、严格前后14组账本同hash、完整通知租约、旧shadow实际stop与新restart boundary、最终动作前新鲜性门均已准备；当前无实际停启结果。
- 原5173只读静态GET返回200并含修正退出文案；原前端build通过。未加载可能触发账户GET重估的整页，不冒充浏览器全交互验收。

## 真实部署环境回归与纠正
旧修复报告1387通过/1跳过来自final-env-round28独立回归环境，不是实际部署解释器。新增补验使用原PID映射的global Python，于21:37:58–21:39:31完成同25模块：1387 passed、1 skipped、15 subtests passed、1已有Starlette警告，exit0；网络/生产DB禁止，blocked_operations=[]。

重要：测试前后源码自身稳定，但随后与20:21发布scope比较发现上述TLS变化；所以这套结果不能被解读为“原冻结739文件仍未变”，也没有运行新增TLS专项测试。仍不能发布。

补验v1因自写隔离器错误解析SQLite fixture只读URI出现5红测，完整保留；中间未正确替换guard的一轮立即中止保留。修正仅测试隔离器，先16项允许/拒绝边界测试再完整重跑；没有放开生产DB、网络或外部写入，不删失败证据。

## 另列的既有运行问题
发布前日志已有：20:25每日复盘缺run_review模块；旧临时CA路径消失导致部分新闻/数据源失败；2000正式预测快照时钟校验失败持续重试；21:00日K有15价格链冲突/8非法OHLC、21:20历史K线被隔离。上述不是本轮发布引起（本轮未重启），也不因health=ok或任务已返回就认定解决。新增TLS改动看起来针对临时CA持久化，仍须单独审验与范围确认。

## 继续与回退边界
1. 确认是否把新增TLS纳入受控候选；不要覆盖并行修改。
2. 若合并，建立新的候选scope/源差异、专项隔离测试与独审，保持E/E2之外执行身份不变，再取新鲜账务/待单/通知并签认。
3. 单次正常重启原8000服务；任何新漂移/进行中状态/边界缺失/账务差异均停止，不自动SIGKILL、补发或回滚运行库。
4. 发布后仍需下一真实交易时段按版本验证模式确认、跌停/一字/模式变化拒绝、订单及退出。natural_forward_accepted=false，不承诺改善收益。

本轮三个已结束/中止回归的tmp夹具目录已逐文件登记inode/大小/时间、核对无打开句柄后清理；成功/失败/中止日志、结果、脚本、清单全保留。未复制或删除运行库，见fixture_cleanup_plan.json及fixture_cleanup_result.json。

主证据目录：outputs/september_strategy_release_20260928/。阻断source_drift_block.json；账务economic_accounting_baseline.json；新窗口window_2132.json；回归source_gate/DEPLOYMENT_REGRESSION.md；独审independent_safety/FINAL_REVIEW.md。早期红测试与原冻结均保留。

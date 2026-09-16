# 第16轮：逐委托成交轮次推进与上轮联合验收

实际工作日期2026-09-15凌晨；文件名沿用9/14复盘目标。**源码修复，不是线上已部署或收益验证。**

## 1. 上轮缺口验收

父已逐段读取独立代理三个实际测试文件与docs/pending-fixture-adaptation-round15-20260914.md、/tmp/claw-round15-adaptation-final.txt（113 passed/15.52s），随后独立完整联合 **2581 passed/335.09s，无skip**（bash-252），恢复上轮尚未验收的34个T回补、continuous、deferred confirmation fixture情景；上一轮初次61失败中的其它27项已由父先前修复。

适配只在实际需要进入成交/假broker拒绝的测试支路显式提供本地full日历、健康Tencent QuoteRound、同一owned帧三钟/版本和固定验收钟，不mock新validator。原T身份、TTL、候选、12账户隔离、真实账本T+1拒绝及费用/持仓/日志断言保留；原quote调用次数准确增加一次查询后的时效复核，不删计数。文件为：
- backend/tests/test_pending_t_buyback_validity.py
- backend/tests/test_continuous_paper_experiment.py
- backend/tests/test_deferred_buy_confirmation.py

上述2581组是在本轮新增轮次推进补丁前启动，不能当作补丁后的最终结果。

## 2. 新复现与修复

**真实隔离复现**：同一300股委托先B轮100股、C轮100股后，再给B轮或换新ID但保留旧/不推进源钟，原代码继续填剩余100股；若旧partial的冻结合同或SHA已缺/坏，原代码仍可新填单。新测试最初 **14 failed/2 passed**：8个买/卖重放情景实际返回filled，6个缺/坏历史情景实际返回partial，两个合法向前对照通过。未连接生产交易API，不能据此指控生产已发生相同成交。

仅本轮新增生产修改：**backend/app/trading/paper_public_execution.py**。
- 新_pending_round_progress复用原_order_fills读取本委托回报，no_autoflush，不修补任何历史。
- 校验每笔pending_execution_timing与ledger_execution_timing的版本/status/SHA、订单/账户/证券/方向/价量/原decision、原请求fill_id及before_mutation时钟关系。SHA用于关联一致性，不宣称防恶意代码篡改或公钥签名。
- 禁止消费任何已出现的fill_round_id；不同ID也必须使源/接收/提交三钟严格大于本委托所有已消费轮次。已有历史内部顺序必须一致，当前观察不能早于既有逻辑成交时间。
- prior filled quantities必须与订单累计数量及last_fill_round_id一致，本笔不能超过原委托总量；缺回报、缺冻结证明或损坏身份保持waiting及原经济证据，不用最新行情推断旧成交。
- round_progress单独标识per_order_fill_progress_v1_20260914，保存先前笔数/量、最后回报与三钟。不是跨账户共享深度证明；不嵌套复制整个历史。
- 所有新增DB读取仍在真实dispatch clock重采样之前；原锁后/before_mutation时钟、原子事务、COMMIT未知传播和CAS委托检查点均未改动。补丁不改新单信号或生产买入阈值，不自动撤改历史部分成交。

对未来模拟结果的影响：旧行情重复消费或无历史证据的余量会等待/拒绝继续撮合，不再生成原本缺依据的填单；没有把缺数据强改成“可成交”。首次待单原decision QuoteRound的完整存在性/源钟关系不在本补丁范围。

## 3. 验证

新backend/tests/test_paper_pending_round_progress_20260914.py共24例：
- 买/卖B→C→B、换ID旧钟、源钟相等/倒退、合法D轮完成。
- 原合同缺失/时钟缺失/SHA冲突、累计量不符、last轮次不符、删除旧回报、旧回报证券错误。
- 每次失败前后用新session读取全部经济账本/费用/回报/持仓叶值，确认旧证据保留；预检诊断写入不冒称零写入。

阶段结果：
- 补丁前新16例：14失败/2通过（bash-253）。
- 新16+时钟106+原子133+CAS44：**299 passed/99.34s**（bash-254），含真实账本、原键重试、rollback、ack未知及竞争拒单。
- 扩展新24+代理三个文件113：**137 passed/19.64s**（bash-255）。
- 最终完整联合 **2605 passed/331.22s，无skip**（bash-256），包含新24例、上轮所有待验收fixture及风险/交易API/paper/pending/Challenger组；仅既有multipart弃用警告。对应最终源码输入SHA/AST及日志逐字节核验记录于outputs/repair_validation_20260914_round16/final_manifest.json。
- 所有组有重叠，不相加作唯一用例数。pytest仅临时/内存SQLite，关闭归档/调度，原冻结复盘库只读；无前端变化，无本轮构建/交互结论。

本轮另取得一次前台定点只读审查：覆盖public_execution:143–327、authorization:187–287和service两处timing_json传递，未发现可证实的新增明显漏洞。审查者未改文件/跑测试/访问DB或网络，不将此结论当上线或完整交易证明。上轮迟未回收的只读后台代理已请求停止，不把未送达报告计入验证。

## 4. 运维与剩余边界

01:18:39原8000仍PID67367（9/14 20:06:43），schema030；query_only=1同快照读110交易/2258委托/293回报，完整交易SHA84b90775519aabae3704f22e820a632ffb1a6a9937413403470b862a0a01c357与前轮相同。当次submitted/partial paper委托为0；只是该时点只读数量，不保证下一交易日没有待单或旧partial风险。

尚未受控部署本轮与此前未发布修复，031仍待统一迁移/发布；同PID不证明懒加载源码版本。未下单、未停服/重启、未迁移、未回填历史。旧partial若缺本合同会失败关闭；不得为恢复余量补造旧证据，应按实际状态明确撤余量/新决策的人工或已有规则流程，而不是本补丁自动处理。

仍需独立修复/研究：跨订单/跨账户五档容量与queue开板量、封板累计量共享/FIFO近似；锁后最新证券身份和完整风险、原decision轮次全集、物理COMMIT及跨进程证据约束；共享因子/K线全消费者与不可变全链原因、同预算时间外容量/退出研究；031受控发布和真实新交易日前向采证。总目标保持active，不宣称全部工程/策略目标完成。

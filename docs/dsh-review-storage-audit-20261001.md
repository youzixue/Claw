# DSH 复盘时间切换与存储审查（2026-10-01）

## 已生效的调度

上海时间仅保留两项：盘前 **08:00**，盘后主复盘 **15:30**，周一至五触发，已存日历确认休市则跳过。
原盘后任务 schedule-dc7d5659-6fe2-4656-b9ac-71cf633fea10 原地更新；未保留21:45，未创建20:55。
盘前任务 schedule-a9739a9f-4a09-483f-aaf6-0352ee1dfa80 仍为08:00。
固定 as_of、任务 prompt 与[共享协议](<../.dsh/skills/ashare-daily-review/references/automation-contract.md>)已同步。

15:30早于Claw自身15:45业务终态、15:50研究出版和20:35盘后快照，不能只移动任务时间而要求全部ready。
[只读门禁](<../backend/app/review/evidence_readers.py#L97-L108>)现从15:30允许研究；缺失材料仍返回partial。
只读取截止前已有成交、通知、轨迹及行情，不等待、补采、提前结算或调用出版器。
晚间是否改代码由人工选定事项，不新增重复自动复盘，不自动改策略。

## 实测占用：不是“可安全删除/可释放”清单

以下为2026-10-01约11:15–11:24只读元信息统计。逻辑字节与du口径分别列示，不能相加冒充实际可释放空间。
APFS共享块、运行占用、唯一证据及恢复依赖尚未做退役认证；本轮没有删除任何已有数据。

| 路径 | 观测 | 性质 |
|---|---:|---|
| [DSH报告目录](<../outputs/dsh_reviews>) | 初次扫描13文件，113,072逻辑字节，约110KiB | 本次自动化输出，含小型验收凭据；后续本轮新增2个微型测试凭据 |
| [既有paper研究目录](<../outputs/paper_research>) | 30文件，133,360,158逻辑字节，约127MiB | Claw既有producer，非DSH触发；最大单报告8,142,544字节 |
| [行情归档](<../runtime/quote_rounds>) | 17日分区，3,635,028,529逻辑字节，约3.39GiB | 既有采集归档；compact约2.02GB、focus约0.586GB、minute约1.03GB |
| [候选影子记录](<../outputs/strategy_candidate_shadow>) | du约21GiB | 仍在使用的持续记录；不按临时副本清空 |
| [旧下午修复目录](<../outputs/afternoon_repair_20260922>) | du约58GiB | 含历史演练/备份等；未认证为可删除 |
| [旧预测发布修复目录](<../outputs/prediction_publish_repair_20260921>) | du约26GiB | 历史研究目录；未认证为可删除 |
| [全部项目输出](<../outputs>) | du约115GiB | 包含上述项目，不能重复相加 |
| [Harness会话持久化目录](</Users/youzix/.dsh/sessions>) | du约409MiB | 所有会话/工作区总量，不能全部归因于这两个任务 |

运行中的[交易数据库](<../backend/claw.db>)du约39GiB及其WAL/SHM均未复制、删除、压缩或VACUUM。
系统df当时显示约108GiB可用；这是当时快照，不是完成清理后的释放量。

## 新DSH流程是否会重演几十GB整库副本

**当前证据不支持它会复制整库；但也不能保证所有相关存储永不增长。**

- [宿主门禁](<../.dsh/plugins/claw-research/automation-policy.mjs#L37-L45>)禁止无人值守回合的shell、任意写、代码修改、回放/采集/任务管理；报告只能经专用保存器落盘。
- [独立连接](<../backend/app/review/evidence_store.py>)使用SQLite mode=ro/query_only，不备份数据库。
- [市场批读取](<../backend/app/review/market_batch.py>)流式读取已有有界归档，不新复制整日分钟文件。
- [研究文件消费者](<../backend/app/review/research_artifacts.py>)读取已有出版文件；16MiB是读取预算，**不是producer的磁盘配额**。
- 两个prompt及共享协议新增“禁止整库副本、回放库、全量导出、分钟复制、大型累计快照、抓包、全量日志dump；每回合最多一个新报告，重试幂等复用；不换路径绕过保存失败”。这是任务行为约束，**不是新增了硬磁盘总配额或自动删除**。

### DSH报告保存器仍有边界缺口

[保存器](<../.dsh/plugins/claw-research/automation-runtime.mjs#L40-L135>)限制正文与证据输入各128KiB、缺失/工作项合计64KiB；同phase/day/输入指纹幂等，材料变化追加revision。
输入额度合计320KiB不等于最终文件大小：JSON转义与pretty-print会增加字节。
400KiB单文件及同阶段/日200份目录限制目前只在reader，writer没有对称的最终payload/数量限制；也没有TTL或目录总量硬上限。
因此存在“写得出但读不了”及长期累积风险，本轮没有把提示词限制冒称成硬配额。

### 既有paper producer存在累计快照增长

[发布器](<../backend/app/paper/research_reports.py#L30-L110>)每天由[原调度](<../backend/app/data/scheduler.py#L1099-L1113>)在15:50/20:45出版，手动重跑还可新增；DSH任务没有调用它。
signal_portfolio每次保存实验起点到当天的累计信号，post_exit保存当日评价和归档引用，不复制完整数据库或完整分钟归档。
时间戳生成新文件，没有跨时点内容去重、大小硬上限或retention。
若每日新增样本量大体稳定，“每天重存累计历史”总量可趋于O(天数²)；文件超16MiB还会使当前消费者失去可读证据。

### 行情归档本身可达到几十GB

[写入与保留](<../backend/app/data/quote_round.py#L338-L450>)：每行情轮次写compact及可选focus；minute同分钟原子覆盖，不因DSH读取再复制。
源码默认compact/focus各保留60日分区、minute保留370日分区；按目录数轮转，不是字节硬预算。
仅成功写归档时每天执行一次，删除错误被ignore，不能视为必然成功的轮转。
9月30日观测：compact145,481,758B、focus39,172,844B、minute64,417,593B。
以该日量和默认保留量推算约34.9GB（32.5GiB），是估算，不是已认证live配置或存储硬上限。

Harness会话日志及工具溢出文件另属宿主持久化，未核实长期轮转策略；不能用项目小报告额度保证它们零增长。

## 已做验证与改动范围

- 只修改研究门禁、对应隔离测试、共享协议及文档；未修改Claw业务结算/出版/行情保留配置、交易参数、订单或账务。
- 主机MCP最初仍用旧缓存，在15:30返回blocked_before_close；仅重新加载该只读MCP插件后，同日期/截止实际返回partial。
  2026-09-30 15:30：日K兼容投影可描述，12户终态均未可见、研究/快照缺失。未伪造ready。
- 新增15:29:59/15:30/15:45三边界测试，验证迟生成快照及晚终态不被提前纳入，源临时库哈希不变。
- [本轮无联网测试凭据](<../outputs/dsh_reviews/verification/1530.tmkhghwx/report.json>)：67 passed，network_attempt_count=0。
  所有测试使用专属TemporaryDirectory，测试库/fixture在完成后清理；只保留小型结果JSON及空事件文件，没有复制运行库。
- schedule_list精确回读仅08:00与15:30两项，均Asia/Shanghai、scheduled；没有21:45/20:55。
- 这是配置与受控事后验证，**不等于15:30自然定时任务已成功**；当日休市仍应跳过。

## 待人工选定的治理建议（未执行）

1. **DSH保存器**：增加最终序列化字节上限、writer/reader一致的revision额度、目录总量告警/硬停；同输入复用必须仍可读，不能靠删除旧结果绕过。
2. **paper producer**：每日增量/有界摘要＋共享不可变明细引用，同输入去重；保留原哈希、读接口和失败/排除分母，另审批保留策略。
3. **行情/候选影子**：分层压缩、磁盘阈值、轮转失败记录与重试；缩短证据保留窗口需明确批准，不能以减少磁盘为由破坏唯一失败对照。
4. **旧大目录**：依据[既有存储政策](<research-storage-policy.md>)列精确文件、用途、唯一性、依赖与打开句柄再申请退役，不擅自删除备份或数据库/WAL。
   逐路径结论见[现有文件退役评估](<dsh-file-retirement-assessment-20261001.md>)：体积前三名均为9/24已授权保留项，当前无必要删除。

本轮没有自动清理任务、重启Claw、生成全库副本或执行publisher。剩余增长风险已明确列出，尚未宣称治理完成。

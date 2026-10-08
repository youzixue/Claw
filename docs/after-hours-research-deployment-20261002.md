# 腾讯/新浪盘后研究受控部署结果

日期：2026-10-02，Asia/Shanghai。用户16:35要求部署，16:47再次确认非交易日维护窗口；范围仍是盘后强弱与次日研究，不是盘后成交或实盘授权。

## 实际上线

- **17:05:15原服务上线**，`127.0.0.1:8000`，原监督者`gui/501/com.claw.dev.backend`；PID80654→15132。切换观测耗时2.936秒，不声称零停机。前端5173的PID1218未变，没有启动替代服务。
- 原库精确从037升级到**038研究迁移**：新增一张不可变观察表、两个查询索引、三个保护触发器。没有升head、没有stamp、没有039/040撮合资源表或历史回填。
- 运行中已核对腾讯/新浪版本、`AFTER_HOURS_RESEARCH_SOURCE=tencent_sina`及204个已导入app模块的发布路径/SHA。该集合是启动时实际导入集合，不冒称259个文件全部业务分支都已运行。
- 原54项作业保留，首次上线已交付的三项研究采证作业，现57项：15:01常规基线、15:35盘后独立量额、20:10复核。没有上线人工意图失效作业、撮合定时器、实盘接口，也没有新建DSH提醒或长期目标。

证据：[受控切换结果](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/restart_result.json>)、[实际加载清单](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate_runtime_15132.json>)、[部署后验证](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/verification.json>)。

## 发布包隔离与交易边界

普通重启会加载整个未部署工作树。因此本次使用[9月30日已上线基线](</Users/youzix/WorkBuddy/Claw/outputs/buy_point_release_20260930/manifest.json>)生成研究专用冻结发布包，不覆盖或回滚用户工作树。

候选共259个Python文件：仅三项既有模块发生研究差异（settings、scheduler、stock model），增加五个必要研究模块。交易服务、授权、持仓规则和API保持9月30日已上线字节版本；排除11项既有工作树并行变更、撮合新模块、人工意图失效调度及八处无关时区改动。发布单元另有显式原配置/存储路径绑定，不改变业务参数；旧54项作业时区行为保持原版。

[候选范围](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/scope.json>)及[研究差异](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate_diff.patch>)保留。原270个app工作树文件指纹未被本次部署改动。

**当前服务从冻结发布包加载，不直接加载工作树。** [发布单元清单](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate/source_manifest.json>)与[启动器](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/launch_release.py>)是实际运行依赖，不能当普通outputs研究结果清理、移动或覆盖。后续改工作树不会自动生效；不要用通用dev_services restart绕过此隔离，或盲目恢复原启动命令加载全部并行交易变更。

## 数据与配置保全

迁移后、加载后均与迁移前核对：

| 保护组 | 行数 | 完整有序全字段内容指纹 |
|---|---:|---|
| 账户 | 14 | 相同 |
| 持仓 | 129 | 相同 |
| 交易日志 | 272 | 相同 |
| 订单 | 2490 | 相同 |
| 成交回报 | 455 | 相同 |

待处理订单为0。此结论限五组账务，不宣称市场/新闻等所有表在正常运行中没有自然写入。

原有schema正文全部一致，仅增加038的六个非自动schema对象；研究表0行，没有把9月30日报价补写成当时可见证据。[原环境文件](</Users/youzix/WorkBuddy/Claw/backend/.env>) SHA不变；实际LaunchAgent只改变启动命令，其他监督参数及原EnvironmentVariables不变。合并**原launchd环境覆盖**后，候选、回退包与工作树的既有业务设置指纹一致，也匹配实际新进程配置指纹；不以未合并监督环境的离线指纹替代运行配置。

只保留小型schema、账务hash、源码/监督配置回退材料；没有复制约41GB整库，没有删除WAL/SHM。证据：[迁移前](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/before_snapshot.json>)、[迁移后](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/after_migration_snapshot.json>)、[加载后](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/after_restart_snapshot.json>)、[随访](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/followup_snapshot.json>)、[监督环境配置比对](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/business_settings_launchd_equal.json>)。

## 验证与已知异常

- **确切发布包研究回归189通过、1 deselected，9.59秒，exit0**。所用临时SQLite已由测试guard隔离。未部署的THS尾字段用例被排除；免费源测试的逐笔目录断言因撮合模块未纳入发布包而未收集。测试范围明确保存，不冒称旧405项全部在此发布包再次通过。
- 成功迁移与故障注入均在小型临时库演练：异常时DDL及Alembic版本一起回滚到037、没有残留研究表。真实迁移使用同一SQLite显式BEGIN IMMEDIATE机制，仅执行038。
- 候选和冻结回退包禁调度lifespan、root/health200检查通过，均用临时库，无监听端口。
- 真实现库只读研究表/次日上下文读取通过：休市日仍closed、前一交易日研究记录为空；没有手动生产采集、扫描、回放、下单、账户刷新或测试推送。
- 首次测试包装器在测试本身189通过后，因重复conftest临时目录判定过窄而exit1；修正核对实际guard目录后exit0。迁移演练先因扫描到未纳入发布包的039导入失败，再因自动索引计数口径失败；限定冻结迁移目录至038并排除SQLite自动对象后成功。均发生于临时库，未因此改变业务实现或放宽研究判据。
- **既有候选影子研究worker异常保留**。原进程关闭出现candidate shadow shutdown incomplete/failed及一个resource_tracker semaphore warning；原PID实际退出，新原服务启动完成。新health的candidate_shadow仍stopped_incomplete，不能宣称完整drain或全模块无异常。本次没有修改封印、修复worker或清空历史告警。

证据：[研究测试结果](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate_tests_v2.txt>)、[精确测试范围](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/candidate_test_scope.json>)、[真实迁移记录](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/migration_applied.json>)、[启动/关闭日志](</Users/youzix/WorkBuddy/Claw/outputs/after_hours_research_release_20261002/restart_lifecycle_excerpt.txt>)。

## 回退与剩余验收

已留存9月30日冻结代码回退单元及原监督配置。本次没有实际执行生产回退。需要回退时，应精确停原监督服务，将同一启动器模式改为rollback，保留原端口、配置、数据库及038新增证据；不能恢复原工作树启动命令来加载未确认交易改动，也不能直接downgrade或用旧库覆盖新数据。另行核对源码、监督配置、账务后再恢复原授权调度。

**自然交易日未验收。** 今天及休市日即使Cron触发，采证仍受确认开市日历闸门拦截。 存储日历显示10月8日、9日开市；预计首个自然采证窗口为10月8日15:01/15:35/20:10，随后交易日10月9日08:00研究读取。没有为此新建提醒；原次日研究链路继续使用既有安排。

免费源仍不是逐笔/队列、主力净流入或次日上涨证明；首次可用钟、全市场覆盖、预算耗尽/失败比例及翌交易日样本完整性待自然观察。来源为研究活跃度，不自动打强买强卖标签、调权、策略晋级或授予交易资格。旧长期目标未恢复，盘后撮合与实盘未开启。

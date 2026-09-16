# 2026-09-14 原8000受控更新记录（进行中）

## 边界
- 仅Claw原后端127.0.0.1:8000，原前端5173/PID21381保持运行。不涉及DSH GUI3101。
- 不用会清端口/kill-9/重写配置的全服务restart脚本；保留原实验/推送环境，不降低任何买入门槛。
- 不访问可能写账本/生成预测的业务GET，不手动下单，不回填错过的正式批次。
- 更新计划：冻结源码→停后端监督者→确认无写者→冷一致性备份→副本升级与启动演练→原库精确迁移→禁调度smoke→另行决定恢复调度。迁移失败不stamp、不downgrade删证据。

## 已执行
1. 已收齐所有源码分支（当前另一个代理仅只读启动审计），指数历史写保护101/117项回归通过。
2. 私密归档目录logs/repair-deploy-20260914，权限700；源码tgz、原plist及backend.env副本600。不在报告公开配置内容。
3. 候选源码归档SHA256：e525b21dc9658d35e2d6be5cfcca3d467bbe6a40bb6fe605f537cdc412863087（19:47:28生成）。含当时app/alembic/测试/前端/脚本，后加的只读审计工具另归档，不把它误称已在此包。
4. launchctl bootout gui/501/com.claw.dev.backend 成功退出码0；确认KeepAlive后端监督者已卸载、8000无监听、生产DB无打开句柄。未kill-9；前端仍原PID。
5. 冷备主库+残留WAL+SHM集合已完成（19:51:25，bash-145 exit0）。主库原件/副本SHA256一致：8d8eda1632a87a35e079efa3c31d151672058fa55978af0da2a9cedfb1db6bd3。主库12,995,723,264 bytes，WAL65,586,312 bytes，SHM131,072 bytes；**主库哈希不代表WAL内交易已包含，必须保持整个备份集合**。已开始核对WAL/SHM及恢复副本quick_check/只读关键表摘要。此时后端暂不可用，不是“部署完成”。

6. 冷备WAL原件/副本hash一致：1bf313a68776c3d6d3d321a57ae65477e102ec629e0a375c32231bdbf86fd1e4；SHM一致：f839e7aee92f729ff862c2e8313763d740e3b501224d48dd0291fce2751670d1。恢复副本保留WAL集合，quick_check=ok；首次只读审计74表计数/27关键表全字段内容摘要完成（bash-146 exit0）。
7. 补上复核发现的60秒指数fallback及StockDaily写边界日期保护，200项交叉通过。最终冻结release SHA256：272081acb93a03d191723210bdd03215a35ee620c94833289cbecc59177140c8（19:56:10）；与先前candidate分别保留，不覆盖旧包。
8. 原backend plist已准备为显式Python3.11/绝对原DB/CLAW_DISABLE_SCHEDULER=1，去除unset，保留其它实验/推送覆盖，新版本标记src-review-272081acb93a03d19172。plutil和环境保留断言通过；尚未bootstrap。
9. 恢复副本精确027→028→029→030升级已执行；禁调度完整lifespan/health通过（不是起新服务器），实际6个触发器SQL均核对含BEFORE UPDATE/DELETE及RAISE(ABORT)。仍在完成迁移+启动后全量27核心表摘要对照，生产继续停写未迁移。
10. 只读代理重新定点复核，确认父新增60秒fallback/StockDaily写边界已关闭已定位阻断；未再确认该限定路径存在历史原字段改写。正常到期结果结算、当前元数据和原有paper生命周期不等于本次手工下单，不把它们作为永久关调度理由；实际放行仍须父验收。

11. 原主库及WAL在迁移前再次完整SHA256匹配冷备；无后端监听/DB句柄后，原库精确升级到030，六触发器体检查通过（bash-151 exit0，20:00:45前已收取）。未执行任何降级/数据修复或业务endpoint。
12. 发布包对当前app/alembic **277个实际文件**逐个字节hash一致；首个检查把bsdtar自动AppleDouble资源叉当磁盘文件而报FileNotFound，定位后仅排除13个生成元数据条目，未跳过真实源码失败。原release包不变。
13. 原launchd标签bootstrap退出码0。首个紧随启动的HTTP读取connection refused；复核监督者runs=1、never exited，随后原8000在**20:01:54**健康成功，新PID **67251**，scheduler.running=false、job_count=0，formal_window_recovery_v1/operational_health/kline_observations均已加载。判定为启动就绪时序，不忽略错误或把首个失败算成功。
14. 原生产库迁移+禁调度启动后与冷备恢复基线：**原74表计数、27关键表完整摘要一致，新表为空，030与六触发器齐全**（bash-152 exit0）。包含股票K线8,745,863行、行情、新闻版本、预测、订单、成交与模拟账本的选定27表全字段摘要，不是抽样。其余47表只核对计数，不夸大全内容校验范围。此后才考虑恢复既有调度。
15. 已核对旧自动日调权配置仍为false，当前调度复盘分支不会自动调整生产类变量；影子流程automatic_promotion=false。没有手工调整模型/门槛、调用业务GET或下单。

16. 禁调度验证实例67251已正常bootout（153 exit0），保存禁调度维护回滚plist并核验其flag=1。原配置仅改flag=0恢复既有调度；确认8000无旧监听后在同一launchd标签bootstrap，未启第二服务/未调用force业务生成。等待新健康与真实前向批次状态。

17. **20:10:28只读窗口复核**：1510仍0尝试/missing（按要求不补）；2000已由恢复器真实完成：run86为20:06:50开始屏障，run87 as_of20:07:24、completed20:08:25，共1486 snapshots（1442首板、44二板）、17 ranked、5 actionable。recovery已already_completed。这是正式新批次持久化，不是策略/收益/逐路线全部通过。
18. **新暴露的合同缺口不能掩盖**：run87 batch_gate_passed=true，但诊断为invalid_persisted_attempt/unknown_candidate_route：news_catalyst_start、oversold_reversal_start、pre_board_probe_start未在统一required-route质量契约声明，门禁unknown；auction_surge_start因auction_data blocked；mainline/second_board passed。应下一阶段核查既有路线及其独立门禁再补统一契约，不能靠把unknown硬改passed、降低阈值或回写run87修绿。
19. 恢复调度后只读订单/成交/模拟成交/账户count仍分别2258/293/110/13，与维护前一致；这次只是count检查，不冒充全部字段不变。全字段一致的严格证据属于前述禁调度迁移/启动验收。
20. 父本轮后台140–156均已收取，退出码全0；另两次前台验证失败分别是AppleDouble非源码条目与极早HTTP就绪时序，已定位并复核，未隐瞒。前端未重启，未手工下单、晋级或调权。

## 后续验收与限制（目标仍active）
- 已完成：恢复副本quick_check=ok，精确027→030升级与完整禁调度lifespan；原74表计数、27核心表全字段摘要一致，新增表为空（bash-150 exit0，19:59:20前已收取）。不是抽样比较。原主库/WAL再次哈希核验后才执行生产迁移。
- 副本init_db无核心历史改写已验收；
- 生产迁移后revision/6个触发器检查；
- 禁调度健康与零核心内容变动验证已完成；**20:07:56原8000新PID67367、scheduler.running=true、49个原任务恢复**（154 exit0），指数窗口通过already_verified复用原health_id484393，不回补旧StockDaily。
- 启动恢复器20:06:49真实发起promotion_2000，当前candidate_build进行中；不把attempt_requested当完成（154）。只读batch-health已实读并归档（155 exit0），未调用生成入口。该进程不是旧21:20破坏式代码，K线/指数保护已随release加载。完整下一交易日行情连续性、风控执行与策略效果仍需前向验收。

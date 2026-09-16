# 竞价证据最小工程修复（2026-09-15）

## 结论与时点

本次为盘中研究材料驱动的**离线源码修复**，不是部署、恢复真实竞价源或策略改参。
研究锚点为 2026-09-15，证据文件
`outputs/strategy_miss_research_20260915_1008/prediction_run_evidence.json`
的 observed_at 为 10:16:38.782191+08:00；主账户补证为
`account_primary_focus.md` 的 10:23:48.653748+08:00。
没有读取生产数据库、调用业务 API、补批、交易或重启服务。

事实：

- 正式 run 89（0925）与 91（0935）均 completed，但冻结 auction_data 质量为
  **0/3002**、blocking。对应前置 run 88/90 是独立 generation_pending 阻断哨兵，不能说整天没有正式结果。
- 两批冻结质量里最新竞价时刻 09:24:23，raw_record_count=232，
  raw_snapshot_count=58，主板 latest_code_count=41，41 行全 unknown，
  feed_complete_count=0、multi_frame_complete_count=0。3002 是应覆盖主板分母，
  不是“已有3002只都形成了前后帧”。
- `account_primary_focus.md` 记载 D1 当天98轮中88轮 auction_data 不合格；
  `account_audit.md` 记载 D2 历史 MARKET coverage_blocked，
  原因缺同股09:20前与09:25后配对。该市场级原因不能绑定每只未买股票。
- 这些事实支持缺失真实源证据/帧覆盖，不支持放松质量门或由未知字段造量增。

## 真实源核查及不能修成什么

实际采集入口在 `backend/app/strategy/auction.py` 的
`AuctionCollector.collect_auction_data`，使用 AkShare
`stock_zh_a_spot_em`，失败切换 `stock_zh_a_spot`。
现有项目明确将其标为：

- source_version=akshare_spot_open_v1_unverified；
- source_quote_at=None，received_at 是实际本机收到时刻；
- price_basis=spot_open_unverified，volume_basis=intraday_cumulative。

这些普通 spot 今开/累计量额不是经过验证的虚拟匹配价格/匹配量。
没有项目已验证字段能够将它们升级为 indicative_match/indicative_matched，
故本次**不改 source、不补源时间、不猜腾讯字段、不将成交量差称为新增竞价买盘**。
已有未知历史行仍未知。

## 已复现且已修复的边界错误

仅修改 `backend/app/data/auction_evidence.py`：

1. `positive_number` 原先只拒 Python bool，NumPy bool_ True 经 float 转为1，
   可以冒充匹配量/价格等正数；现在与项目资金质量函数一致，拒两类布尔。
2. 非字符串 volume_unit（list/dict）原先在字典/集合查询时抛 TypeError，
   中断质量审计；现在换算返回 None，证据状态返回 unknown_unit。
3. 有限正 lot100 原值乘100后溢出，原先单帧 status 仍可能 ok，
   而真正消费时份额换算为 None；现在单帧复用 volume_in_shares 验证，
   返回 incomplete_values。

状态名称、时段、源时钟先后、最大时差、95%完整率、两个真实帧要求及模型参数均不变。
这是畸形输入降级/一致性修复；**没有证据说明这三个边界就是9/15零覆盖的根因**。
修复不能使现有 unverified 源合格，也不承诺 D/D2 产生信号或成交。

## 父任务集成边界（本次未改）

- `scheduler.py` 已有30秒轮询，以及09:20:06、09:24、09:25:06强制点。
  强制点仍受重入锁和采集窗口约束；真实响应越过09:25:30不会倒填。
  是否需要真实09:20前基线保障、采集耗时/超时与优先级调整，由父在自己的所有权内处理。
  单纯增加触发点不能解决当前源时钟与语义均不合格。
- D2 `strategy_iteration_shadow.py` 当前按同股分组，用单帧 status 判有效，
  不另验同源同版本；最终帧按 auction_time（接收/观测时刻）选取。
  若将来接入多源真实匹配数据，应另核对 source/version 连续性与源时序，
  避免不同来源独立更新被当作同一路径；不能回退旧合格帧掩盖新失败。
- D2 baseline_volume/final_volume 当前是带各自unit的原值快照，
  此处**没有实现量差交易门**，不能宣称本次修了一个已存在的量差算式。
  今后若需要量差，必须先按声明单位转股并核对匹配量语义；
  即使同口径量差也不证明净买/撤单行为。
- D 主线普通“两帧健康”与 D2 “09:20前＋不可撤单阶段＋09:25后”是不同合同，
  不把两者计数混为一谈，也不以改写健康统计恢复旧不可变批次。

## 离线验证与文件

新增 `backend/tests/test_auction_evidence_repair.py`，
13个纯隔离用例覆盖布尔、畸形/未知单位、换算溢出、等价单位正常值及未知源保持阻断。

使用已有环境
`logs/repair-rehearsal-20260915/final-env-round28/bin/python`，
通过 `backend/scripts/pytest_no_network.py`：

- 修改前：新增文件 **5 failed / 8 passed**，复现上述三个错误；network_attempt_count=0。
- 修改后：新增文件 + `test_auction_provenance.py` + `test_auction_data.py`，
  **58 passed**，network_attempt_count=0，no_network_gate_passed=true。
- 输出：`logs/repair-rehearsal-20260915/auction-repair-before.json`、
  `auction-repair-before.jsonl`、`auction-repair-after.json`、
  `auction-repair-after.jsonl`（空events文件表示未见联网尝试）。
- DB fixture 将所有测试数据库强制置于临时目录；真实来源调用由已有fixture隔离。
  网络审计覆盖pytest主进程和线程，不宣称对子进程的通用安全沙箱。
- 仅有既有 pytest_asyncio event_loop/默认scope弃用警告，未因本任务调整共享conftest。

交付只涉及证据工具、新测试、新文档及上述测试日志。
保留并行脏改动；未改 scheduler/settings/paper/promotion/ORM/migrations/main/frontend。
源码验收不是运行进程已部署，后续真实帧需自然到达并通过原门禁，新正式批次才能取得新证据。

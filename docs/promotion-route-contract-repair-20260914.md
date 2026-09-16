# 正式预测逐路线质量契约修复（2026-09-14，第5轮）

## 事实、影响与边界

本轮核查发现：合法候选路线全集与数据质量依赖表混用。旧表只列 B/C/D 三条，但正式生成器还会产生新闻、试盘、反包等路线。合法名称缺冻结 gate 被误报为 unknown_candidate_route；这是身份诊断缺陷，不等于这些路线之前没有独立形态/新闻/执行检查，也不证明它们应该被买入。

源码新增纯共享 `backend/app/promotion/route_contract.py`，由原有质量审计、生成持久化、只读诊断、B/C/D消费和隔离配额研究复用；未增平行评分器或订单入口。共享大文件仅局部修改。概率、排名、模型Champion、阈值、交易账户路线、T+1/可成交/风控不放宽；输入门收紧可能减少候选可消费数量，不宣称收益改善。

## 新冻结合同

- 版本：`promotion_route_quality_v1_20260914`。
- 合法名15个，其中当前生成主体13个；`fresh_hot_start/mainline_relay`为保留原值的历史兼容名，不按今天数据重新分配旧路线。
- 基础依赖复用已有真实水位 `stock_kline/fund_flow/limit_up_pool`；auction_surge_start 继续另需 auction_data。所有合法名均声明基础依赖，不将预测输出作为自己的输入。
- 依赖水位缺失、非ok、不达原完整度、NaN/Infinity/越界/布尔等异常均失败关闭。没有新增伪新闻覆盖率或把行数>0当PIT证明。
- `route_contract`冻结版本、完整依赖声明、兼容名、原 B/C/D执行子集，以及独立逐股证据检查的描述。描述不是检查结果，更不是交易授权；新闻的直接实体/原文和分析可见性、逐股K线确认、竞价增量帧仍由原有实际消费者判断。
- 新审计 summary 已带合同，原不可变 run metadata 写入路径原样冻结。相同旧run身份的幂等调用仍返回旧证据，不能借新合同补旧metadata；只有真实新批次获得新声明。
- 官方原始输入在 recordability 过滤和DB写入前检查路线；真非法名称不被筛掉以缩分母。独立 ScheduleBatch 可追加 invalid_candidate_route 阻断尝试，不改旧run/snapshot/兼容行；未独立标记的调用只返回未落账状态。直写ledger也有相同拒绝边界。source别名和候选内schedule标记不能绕过检查。
- invalid_candidate_route 的 recordable/prepared=0 表示未进入该处理阶段，不是市场零候选；派生filtered/invalid_identity计数为null，台账标明 route_identity_before_recordability。

## 历史诊断与实际消费

`batch_diagnostics`升级为v2，拆开：
1. 当前只读路线身份：known / known_legacy / invalid_missing / unknown_illegal。
2. 当时冻结证据：原route gate的true/false/缺失，及 legacy_unversioned / supported / unsupported / 声明损坏。

合法且有快照但缺gate → completed_route_gate_unknown，不再非法，也不改为passed。真实非法/空路线仍invalid，保留分母。未知门优先报告，同时保留各路线已知blocked状态。旧批次从未声明、也无候选的新路线不凭当前全表假装当时评估过。版本不支持或声明被削弱，不能回退旧语义。

B/C/D仍先选允许窗口内最新批次，再验同run冻结路线门，失败不回退旧批次。**本轮取消旧版仅batch gate=true、完全没有route_gates时的交易兼容放行**；明确阻断并要求新正式批次。旧版已有明确逐路线布尔门继续按当时位值读取，标为legacy_unversioned，不重建其历史依赖；后续概率、行情、确认、可成交和风控门仍保持。新版本必须完整匹配合同声明、该路线required_datasets且布尔门不矛盾。未知/损坏门不会被全局true盖过。该冻结失败不能原位恢复，因此未成交旧委托重验时取消而非无限等待；将来新正式批次必须走新的候选/订单身份，不能把新证据借给旧单。本轮未操作任何生产委托。

只读配额研究保留 frozen_route_gate_passed 原位值，新增 validated_route_gate_passed/contract_status；不支持版本即阻断，buy_allowed始终false。研究解释版本为 promotion_route_rank_research_v2_quality_contract，原排名与三臂对照不变。

## 实际run87只读复核（不是部署）

20:29:15.600427，原生产库以 SQLite URI mode=ro + query_only + 显式只读事务读取 run87 及全部1486快照，用本轮源码的纯诊断函数投影：

| 路线 | 快照数 | 当时冻结门 |
|---|---:|---|
| second_board_promotion | 44 | passed |
| mainline_spread_start | 1 | passed |
| news_catalyst_start | 10 | unknown，未声明 |
| oversold_reversal_start | 108 | unknown，未声明 |
| pre_board_probe_start | 1323 | unknown，未声明 |

无候选的竞价路线仍保留 auction_data blocked。整体新解释 completed_route_gate_unknown，结构issues为空，绝不是质量全通过。前后两次读取该run与全部快照全字段摘要一致：

`9b241773fd30e934b663ba849092b0a64aedec14403181de46a16958bfaa6cb0`

证据：`outputs/repair_validation_20260914_round5/run87-route-contract-readonly.json`。摘要协议为Python3.11 repr全字段、有序行；这仅证明本次只读复核未变该run/快照，不能外推全部生产表或全天运行历史不变。初次使用内存数据库隔离导入失败：现有engine配置pool_size不支持StaticPool；未开始DB读取，改用显式只读URI后成功，不修改公共engine配置。

## 回归及源码/上线区分

- 首轮7文件：775 passed / 1 failed。失败是旧诊断fixture自动随当前路线表生成了新闻true门，却断言它缺失；改为显式删除该冻结门，保留“缺失不能通过”原断言。
- 扩展14文件：1133 passed / 7 failed。失败均为generation_barrier的“成功批次”fixture仅给全局true而期待B/C/D可消费；补充该fixture本路线明确gate，不恢复生产batch-only放行。原同秒成功排序/影子超时/失败重试不可变断言不变。
- 五文件定向复测：460 passed / 25.75s。
- 全promotion与纸盘/因子等扩大回归：2376 passed / 64 failed；逐条错误均为同一隔离shadow fixture使用未注册的 test_route，导致正式ledger在影子研究开始前拒绝。仅把该测试文件的三个占位路线改为已注册 mainline_spread_start，不改任何影子身份/PIT/封存结果/覆盖分母/人工晋级规则；随后联合2498 passed / 3 failed，影子64项均已恢复；剩余3项为运行已开始后才修正的父资金新测试旧assert（available=false对象误当None），后续专测332项已通过。最终冻结源码同组重测 **2501 passed / 183.22s**，仅原python_multipart弃用提示。
- 追加旧委托失败关闭断言后，路线合同/概率/仓位交易事务三文件 **399 passed / 13.41s**；测试仅调用隔离候选与确认函数，未提交订单。与2501大组存在重叠，不累加成独立样本。
- 本轮后端源码尚未重新部署：上一轮 PID67367 / 原8000 / schema030 是round4版本，不是本轮契约版本。未调用生产candidate/模拟盘业务GET，未重启、补跑、下单、调权/晋级或修改旧预测。
- 更全面的证券身份/全部K线消费者、前向共享因子证据、候选到成交原因链、未来退出费用守恒及隔离容量/退出研究仍按总目标继续，不以本专项通过冒充全目标完成。

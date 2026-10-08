# 现有文件退役评估（2026-10-01）

回答“现有文件有没有必要删除”。结论：**当前没有必要删除，也不建议删除体积最大的三项。**
本文件只做只读盘点与分类，**未删除、未移动、未连接任何数据库、未重启服务**。

追加（同日 12:15）：用户随后指令“低风险的帮我删”。执行前复核发现初版 §4 候选经不起核对，
**最终删除 0 个文件**，撤回理由见 §4、执行结论见 §6。

## 1. 空间现状：不存在存储紧急状态

| 口径 | 数值 |
|---|---:|
| 数据卷 460Gi，已用 | 325Gi（76%） |
| 可用 | 107Gi |
| 工作区 `outputs/` | 115G |
| 工作区 `backend/` | 39G（其中运行库 `claw.db` 41,326,977,024B） |
| 工作区 `runtime/` | 3.5G |
| 工作区 `logs/` | 1.8G |
| `outputs/dsh_reviews`（本次自动化产物） | 136K |

可用空间按当前进度（运行库约 0.94GB/日，见 §3）仍够数月。没有“不改就写满”的证据支撑立即删除。

## 2. 体积前三名全部是 9/24 已授权清理时“明确保留”的白名单项

三项合计 88,706,928,640B（约 82.6GiB，占 `outputs/` 约七成）。inode 与
[9/24 清理计划的 retained 记录](<../outputs/disk_cleanup_20260924/plan.json>)逐一吻合，说明它们当时就被判定为不可删。

| 文件 | 字节 | inode | mtime | 当时状态 |
|---|---:|---:|---|---|
| [frozen.db](<../outputs/prediction_publish_repair_20260921/frozen.db>) | 27,531,390,976 | 132633722 | 2026-09-21 16:45:15 | retained |
| [standalone-pre-rehearsal.db](<../outputs/afternoon_repair_20260922/rehearsal_backup_v2/standalone-pre-rehearsal.db>) | 30,569,041,920 | 139134964 | 2026-09-22 15:14:17 | retained |
| [stopped-before-035.db](<../outputs/afternoon_repair_20260922/original_release/stopped-before-035.db>) | 30,606,495,744 | 140500631 | 2026-09-22 17:27:35 | retained |

各自性质（据随附凭据，未重开库）：

- `frozen.db` 是 9/21 16:45 冻结基线，被[调度公平性研究](<scheduler-fairness-repair-20260922.md#L30>)作为固定研究钟（2026-09-21 23:35:35）的只读输入。删掉即失去该研究唯一可重放输入。
- `standalone-pre-rehearsal.db` 是 9/22 在线只读一致性备份，`backup_sha256 1b7bc47f…b7d5d44`，`status=backed_up_not_migrated`，页进度 101%、7,463,145 页。属政策“9/22 有效演练输入”。
- `stopped-before-035.db` 是停机前一致性备份，`backup_sha256 71bfdc83…b8cda8`，记录 `integrity_check=ok`、外键违规 0。属政策“停机恢复备份”。

三项都不增长：mtime 自 9/21–9/22 未变，`lsof` 对三个路径及其父目录均无打开句柄，`nlink=1`，非符号链接。
**删除它们不解决任何增长机制，只销毁唯一基线。** 与[存储政策](<research-storage-policy.md#L19>)第 4、5 条直接冲突，因此不列为删除候选。

## 3. 真正在增长的是写入方，不是这些静态文件

| 路径 | 当前 | 增长特征 |
|---|---|---|
| [运行库](<../backend/claw.db>) | 41,326,977,024B | 9/24 记录 34,776,379,392B → 7 天 +6.55GB，约 0.94GB/日；WAL 4,326,032B、SHM 32,768B 都不大 |
| [候选影子](<../outputs/strategy_candidate_shadow>) | 21G / 50,730 文件 | 9-24 14G/10,680；9-28 1.0G/8,943；9-29 6.1G/31,106；9-30 仅 1 文件。按日累积，O(天数²) 结构风险 |
| [行情归档](<../runtime/quote_rounds>) | 3.4G / 17 分区 | compact/focus/minute；轮转按目录数（60/60/370）而非字节硬上限 |
| [paper 研究出版](<../outputs/paper_research>) | 127M / 30 文件 | 每日新增累计快照，无 retention |
| `logs/backend-uvicorn.log` | 105M（另 .gz 51M） | 运行追加写 |

结论：要“处理增长”应给这些写入方加**配额/保留策略**，删静态基线既省不了多少、又破坏可追溯性。
这属于上一轮[存储审查](<dsh-review-storage-audit-20261001.md#L79-L84>)列出的待批治理项，本轮不做。

## 4. 曾列为“低风险”的候选，复核后全部不成立（本节为撤回记录）

本节初版列出约 2.1GB“可选低风险项”。按用户“帮删低风险”的指令做执行前复核时，发现仓库已有更强的保护机制，
**两项主要候选都不低风险，不能删**。撤回原因如下，作为可审计记录保留。

| 候选 | 初版判断 | 复核结论 |
|---|---|---|
| `logs/repair-rehearsal-20260915` 内 venv（约 1.6G） | 可重建，低风险 | **撤回：硬保护。** [storage_governance.py](<../backend/scripts/storage_governance.py#L108-L123>) 的 `PROTECTED_ARTIFACTS` 登记 `final-env-round28`（“实际部署单元，不能按普通日志清理、移动或覆盖”）与 `declared-env-round28`（“保留前两环境作为诊断证据”）；[保护测试](<../backend/tests/test_storage_governance_protection.py#L154-L159>)把 `final-env-round28/bin/python3.11` 锁为保留底线，并注明它是“唯一跑过 99 轮子离线安装 + 2683 测试 0 失败的环境”。实测 `22 passed, 1 skipped`，删除即打破保留底线 |
| `outputs/intraday_repair_20260924` 内字节相同的 `auto_logs_today.jsonl`（4×133,047,583B、3×68,500,437B） | 内容一致，冗余可删 | **撤回：逐路径登记在案。** [release_review.json](<../outputs/intraday_repair_20260924/release_v4/release_review.json>) 以 `路径: sha256` 形式登记 `release_v4/baseline/auto_logs_today.jsonl`，属已签认（`approved`/`signer`/`authority`）复核证据；各 phase 目录内的 `auto_logs_today.meta.json` 亦各自登记 `file`+`bytes`+`sha256`。删除任一副本都会让对应记录指向缺失文件，等于改动证据清单——不是低风险 |
| `outputs/afternoon_repair_20260922/migration/tests-*/work-copy.db` | 3.1M，临时库 | 收益 3.1MB，且与 9/24 已登记哈希的 `audit.json`/`FAILURE_SCOPE.md` 同属迁移失败证据族；不值得为 3MB 触碰 |
| `runtime/wencai-profile/*.db-journal` | 0B | 收益 0 字节，且属 Chromium profile 运行时文件；不碰 |

### 项目自带的治理工具也判定为 0

[storage_governance.py](<../backend/scripts/storage_governance.py>) 默认 dry-run，覆盖 `outputs/`、`backend/outputs*`、`logs/`、`.tmp/` 的演练快照与运行日志。
2026-10-01 执行只读审计，结论：

```
演练快照清单（扫描 .../outputs）
  可回收: 0 个文件，合计 0.00 B
  保留  : 0 个文件，合计 0.00 B
本次可释放：0.00 B
（只读审计模式，未删除任何文件）
```

即：**当前没有任何被该工具认可的可回收项。** 唯一它认为可回收的资源是运行日志，且方式是
`--rotate-logs`（gzip 归档后截断，**保留历史归档、不删除**），当前 `logs/backend-uvicorn.log` 101.49MB。
这属于“压缩留存”而非删除，默认需 `--apply --i-confirm-backup` 显式确认，未执行。

## 5. 禁止删除清单（本轮已核对仍有效）

- [运行库](<../backend/claw.db>) 及 `-wal`/`-shm`：`uvicorn app.main:app`（PID 80654）正在使用。
- 上文三个 retained 基线。
- [core-state-20260916.sqlite.gz](<../outputs/backups/core-state-20260916.sqlite.gz>)（1,122,899,200B）：唯一压缩恢复态备份。
- `outputs/disk_cleanup_20260924/` 与 9/21–9/22 报告 JSON：9/24 已记录哈希不变，删除会破坏可核验性。
- `outputs/intraday_repair_20260924`（2.7G）、`outputs/promotion_root_cause_20260929`（2.0G）、`outputs/strategy_controlled_release_20260929`（421M）、`outputs/dsh_reviews`（136K）：均为近期已结题研究与本次自动化产物。

## 6. 执行结果与建议

**用户指令“低风险的帮我删”已执行复核，实际删除 0 个文件。** 原因不是遗漏，而是执行前核对发现
两项主要候选分别受硬保护与逐路径签认登记（§4），第三、四项收益为 3.1MB 与 0 字节。
在项目自带治理工具判定可回收 0 字节的情况下，继续删除即等于绕过 §5 的禁止清单。

1. **本轮不删除任何文件。** 没有存储压力（可用 107GiB），最大三项受政策保护且不增长。
2. 需要腾空间时，唯一被现有工具认可的动作是 `backend/scripts/storage_governance.py --apply --i-confirm-backup --rotate-logs`
   对 `logs/backend-uvicorn.log`（101.49MB）做 gzip 归档＋截断；这是压缩留存而非删除，且未执行。
3. 若确实要回收 §4 任一项，必须先走“显式放宽”程序：像 `AUTHORIZED_SUPERSEDED_20260916` 那样在代码里登记授权，
   而不是直接 `rm`；对已签认的发布证据，还需要说明该签认是否随之失效。
4. 优先做的是 §3 的写入方配额与保留策略，而不是删基线；删除基线属于“以减少磁盘为名破坏唯一失败对照”。

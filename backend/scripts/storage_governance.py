#!/usr/bin/env python3
"""Claw 存储治理：一次性演练快照回收 + 运行日志轮转。

背景（2026-09-16 复盘定位）
---------------------------
`outputs/` 体积 67GB，其中 65.39GB 是**部署/演练流程的全库 SQLite 快照**
（`continuous-live-*` / `continuous-deploy-*` / `fund-breakdown-*`），
每个流程留一份 9~10GB 快照且从不清除；另有 `logs/backend-uvicorn.log` 达 779MB。

**重要澄清**：这些**不是测试数据**。`tests/conftest.py` 已把 `DATABASE_URL`
指向临时目录，生产库无测试残留。成因是演练脚本的全库拷贝 + 无保留策略。

安全设计
--------
* 默认 **dry-run**：只列清单与可释放空间，不删除任何文件。
* 删除需显式 `--apply`，且必须 `--i-confirm-backup`。
* 默认**保留最近 N 个快照目录**（`--keep`，默认 1）作回滚点。
* 只匹配已知的演练快照文件名（`before.sqlite3` / `rehearsal.sqlite3` /
  `review-before.sqlite3` / `business.before.sqlite3` 及其 -shm/-wal），
  **不做通配删除**。
* 日志轮转使用 gzip 归档后截断，不删除历史归档。

受保护资产（2026-09-16 二次修正）
--------------------------------
首版按「目录是演练目录 → 里面的库文件都可回收」处理，**这是错的**：
`logs/repair-*` 下有多个库文件是受控发布流程**明文登记的证据/回滚单元**，
不是普通演练快照。被误判的至少 48.58GB：

| 文件 | 体积 | 证据 |
|---|---|---|
| `repair-rehearsal-20260915/production-online-030.sqlite` | 12.16GB | 第26轮封存在线备份，SHA `85cf3384…5b6312` |
| `repair-rehearsal-20260915/rehearsal-033.sqlite` | 12.16GB | 第26轮恢复副本（031–033 迁移与 smoke 载体） |
| `repair-deploy-20260914/cold-original/claw.db(+wal/shm)` | 12.16GB | 9/14 冷一致性备份集合 |
| `repair-deploy-20260914/rehearsal/claw.db` | 12.10GB | 027→030 生产迁移演练的恢复副本 |

两道护栏：
1. `SEALED_MANIFEST` —— 从受控发布的机器可读清单
   `outputs/repair_validation_20260914_round29/final-sealed-inputs.json`
   读取 `preserved_artifacts[]`，**按清单保护，不靠人工记忆**；
2. `PROTECTED_ARTIFACTS` —— 文档正文明确要求保全、但未进清单的项。
   清单缺失或损坏时**保守回退到静态列表**，不因读不到清单而放行。

判定用「前缀匹配」：登记目录即保护其下全部文件。
被保护的项一律不出现在可回收清单里，并在报告中单列。

用法
----
    # 审计（只读，默认）
    python3 scripts/storage_governance.py

    # 确认后回收（保留最近 1 个快照目录作回滚点）
    python3 scripts/storage_governance.py --apply --i-confirm-backup
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OUTPUTS = REPO / "outputs"
LOGS = REPO / "logs"

# 需要扫描的「演练/部署备份」根目录。
# 2026-09-16 修正：首版只扫了 REPO/outputs 与 logs/*.log，漏掉了
#   logs/repair-*/、backend/outputs/backups/、.tmp/* —— 实测漏掉约 95GB
#   （用户截图显示 macOS「文稿」占 235GB，与此吻合）。
SNAPSHOT_ROOTS = (
    REPO / "outputs",
    REPO / "backend" / "outputs" / "backups",
    REPO / "backend" / "outputs",
    REPO / "logs",
    REPO / ".tmp",
)
# 目录名前缀：命中即视为演练/部署备份目录（其内的库文件可整体回收）
SNAPSHOT_DIR_PREFIXES = (
    "continuous-live-",
    "continuous-deploy-",
    "fund-breakdown-",
    "priority-repairs-",
    "repair-deploy-",
    "repair-rehearsal-",
    "promotion-rollout-",
)
# 备份文件名特征（不含生产库 backend/claw.db）
SNAPSHOT_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".sqlite3-shm", ".sqlite3-wal",
                     ".db-shm", ".db-wal")
SNAPSHOT_NAME_MARKERS = (
    "before", "rehearsal", "review-before", "business.before", "subset.",
    "pre-migration", "production-online", "claw-pre-alembic", "claw-before",
    "pre_", "cold-original", "claw.db",
)
# 运行中数据库的绝对路径：永不回收
NEVER_TOUCH = (
    REPO / "backend" / "claw.db",
    REPO / "backend" / "claw.db-shm",
    REPO / "backend" / "claw.db-wal",
)

# 受控发布的机器可读保全清单：`preserved_artifacts[]` 里 path/sha256/bytes 即权威登记。
SEALED_MANIFEST = (
    REPO / "outputs" / "repair_validation_20260914_round29" / "final-sealed-inputs.json"
)
# 文档正文明确要求保全、但未进上述清单的项（repo 相对路径；目录即保护其整棵子树）。
PROTECTED_ARTIFACTS = (
    # docs/controlled-release-20260915.md:40 —— 「实际部署单元，不能按普通日志清理、移动或覆盖」
    "logs/repair-rehearsal-20260915/final-env-round28",
    # docs/deployment-repair-20260914.md:5 —— 「必须保持整个备份集合」（主库+WAL+SHM 不可拆开）。
    # 这是唯一一份**冷一致性**备份，也是灾难恢复质量最好的还原源，作为保留底线。
    "logs/repair-deploy-20260914/cold-original",
    # docs/controlled-release-20260915.md:14 —— 最新（9/15 07:09）停写时刻回滚点，保留底线之二。
    "logs/repair-deploy-20260915/pre-migration-030.sqlite",
    # docs/declared-environment-repair-20260914.md:31 —— 「保留前两环境作为诊断证据」
    "logs/repair-rehearsal-20260915/declared-env-round28",
    # 代码回滚用的解包源码目录（docs/controlled-release-20260915.md:11）
    "logs/repair-deploy-20260915/rollback-source",
    # 冻结复盘库：backend/tests/test_paper_accounting_frozen.py 的只读夹具。
    # 该测试把本文件 SHA256 硬编码在断言里（第 23 行），删了此测试永不可能再通过。
    "outputs/postmarket_review_20260914_1717/evidence.sqlite",
)

# ── 2026-09-16 用户授权回收（有文档记载，但已被取代或为派生副本）────────────────
# 用户判据：「不影响系统运行的都可以删」。以下三项虽有文档提及，但：
#   * 均为 **030 快照或演练派生副本**，其内容被更晚的 pre-migration-030 与在用库覆盖
#     （在用库为严格超集：paper_trade_log 133 vs 110、stock_kline 8,756,257 vs 8,745,863）；
#   * 保留底线已由 cold-original（唯一冷备集合）+ pre-migration-030（最新停写点）承担。
# 之所以单列而不静默从 PROTECTED_ARTIFACTS 删除：让"哪条文档约束被用户显式放宽"
# 在代码里可审计，而不是表现为保护清单莫名其妙少了几行。
AUTHORIZED_SUPERSEDED_20260916 = (
    # 第26轮封存 030 在线备份（full-size-release-rehearsal-20260914.md:32，SHA 85cf3384…）
    # 被更晚的 pre-migration-030.sqlite 取代 —— 同为 030，后者晚 1 天 45 分钟。
    "logs/repair-rehearsal-20260915/production-online-030.sqlite",
    # 第26轮恢复副本（同文档 :33）—— 由冷备 + 031–033 迁移**可重建**，迁移验收已完成。
    "logs/repair-rehearsal-20260915/rehearsal-033.sqlite",
    # 027→030 演练恢复副本 —— 同上，由 cold-original 可重建，该轮验收已记录在案。
    "logs/repair-deploy-20260914/rehearsal",
    # ── 2026-09-16 二批：用户选择「精简备份替代旧全库快照」────────────────────
    # 新建 backend/scripts/backup_core_state.py 产出 core-state-<日期>.sqlite.gz
    # （74 张表 / 逐表行数与活库核对通过 / 14.6GB → 564MB）。该备份**是当天的**，
    # 而这两份是 9/14、9/15 的旧全库快照，且与活库同盘、无法防磁盘故障。
    # 用户明确选择：删旧快照，留当天精简备份。此为用户决策，非工具推断。
    "logs/repair-deploy-20260914/cold-original",
    "logs/repair-deploy-20260915/pre-migration-030.sqlite",
)


_OVERRIDE_REPORTED: set[Path] = set()


def _is_authorized_superseded(path: Path) -> bool:
    """路径是否落在用户显式授权回收的清单内（含目录前缀语义）。"""
    try:
        target = path.absolute()
    except OSError:
        return False
    for raw in AUTHORIZED_SUPERSEDED_20260916:
        guarded = (REPO / raw).absolute()
        if target == guarded or guarded in target.parents:
            return True
    return False
# 显式登记的「备份容器」目录（其内部文件直接就是备份，无前缀子目录）
SNAPSHOT_CONTAINER_ROOTS = (
    REPO / "backend" / "outputs" / "backups",
)
MIN_SNAPSHOT_BYTES = 100 * 1024 * 1024      # 小于 100MB 的备份不回收（留作小样本）


def load_protected() -> dict[Path, str]:
    """返回 {绝对路径: 保护来由}，供前缀匹配使用。

    清单读取失败时**回退到静态列表并告警**——宁可少回收，不可误删证据。
    """
    protected: dict[Path, str] = {}

    def register(raw: str, reason: str) -> None:
        # 存逻辑绝对路径，不做 resolve() —— 见 _identities 的符号链接说明
        try:
            protected[(REPO / raw).absolute()] = reason
        except OSError:
            pass

    for rel in PROTECTED_ARTIFACTS:
        register(rel, "文档保全要求")

    try:
        import json

        payload = json.loads(SEALED_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            f"  警告：读不到保全清单 {SEALED_MANIFEST.name}（{exc}）；"
            f"仅按静态列表保护，将少回收。",
            file=sys.stderr,
        )
        return protected

    for item in payload.get("preserved_artifacts") or []:
        if isinstance(item, dict) and item.get("path"):
            register(str(item["path"]), "受控发布清单登记")

    # 用户显式授权的例外：文档/清单要求保全，但用户在带日期的决策中放宽了。
    # 这里**减去**而不是从上面两个来源里删掉 —— 让"哪条约束被谁在何时放宽"
    # 在代码和运行输出里都可见，而不是表现为保护清单莫名其妙少了几个路径。
    overridden = {p: r for p, r in protected.items() if _is_authorized_superseded(p)}
    for path, reason in overridden.items():
        # 每次进程内只报一次：load_protected 会被 find_snapshots 与 main 各调一次，
        # 不去重会让同一条例外刷两遍，淹没其他输出。
        if path not in _OVERRIDE_REPORTED:
            _OVERRIDE_REPORTED.add(path)
            print(
                f"  注意：{path.relative_to(REPO)} 原受「{reason}」保护，"
                f"已被 AUTHORIZED_SUPERSEDED_20260916 放宽（用户决策）",
                file=sys.stderr,
            )
        del protected[path]
    return protected


def _identities(path: Path) -> list[Path]:
    """返回路径的逻辑绝对路径与真实路径（去重）。

    必须**同时**保留逻辑路径：受保护目录里的符号链接（如
    `final-env-round28/bin/python3.11` → 系统 Python）用 `resolve()` 会跳到
    保护范围之外，只用真实路径判定会让这些文件逃逸保护。
    """
    out: list[Path] = []
    try:
        out.append(path.absolute())
    except OSError:
        pass
    try:
        real = path.resolve()
        if real not in out:
            out.append(real)
    except OSError:
        pass
    return out


def is_protected(path: Path, protected: dict[Path, str]) -> str | None:
    """路径本身或其任一祖先被登记为保全资产即视为受保护。

    判定对「逻辑路径 / 真实路径」双向都做，宁可多护一层也不放行。
    """
    identities = _identities(path)
    if not identities:
        return "无法解析路径（保守视为受保护）"
    for guarded, reason in protected.items():
        guarded_ids = _identities(guarded)
        for candidate in identities:
            for target in guarded_ids:
                if candidate == target or target in candidate.parents:
                    return reason
    return None


@dataclass
class Snapshot:
    path: Path
    size: int
    directory: Path


def _is_snapshot_name(name: str) -> bool:
    lower = name.lower()
    if not lower.endswith(SNAPSHOT_SUFFIXES):
        return False
    return any(marker in lower for marker in SNAPSHOT_NAME_MARKERS)


def find_snapshots() -> tuple[list[Snapshot], list[Snapshot]]:
    """扫描所有演练/部署备份目录，返回 (可回收, 因保全而拦住) 的库文件。

    与首版的关键差异（三处均已实测修正）：
    1. 不再只扫 REPO/outputs，而是遍历 SNAPSHOT_ROOTS 下的备份目录，
       并支持多层嵌套（如 logs/repair-deploy-*/rehearsal/）；
    2. **备份目录本身也会被扫描** —— 例如 backend/outputs/backups/ 里
       直接存放 claw-pre-alembic-*.db；首版只认「名字带前缀的子目录」，
       导致这 23GB 被漏掉；
    3. **受控发布登记的证据/回滚单元被硬拦截**（`load_protected`）——
       首版只看「目录名像演练目录」就整体回收，会误删 48.58GB 受保护库文件。

    显式排除运行中的生产库（NEVER_TOUCH）。
    """
    found: list[Snapshot] = []
    guarded: list[Snapshot] = []
    protected = load_protected()
    never: set[Path] = set()
    for path in NEVER_TOUCH:
        never.update(_identities(path))
    skip_names = {"__pycache__", ".git", "node_modules"}

    target_dirs: set[Path] = set()
    for root in SNAPSHOT_ROOTS:
        if not root.is_dir():
            continue
        # (a) root 自身就是备份容器（名字含 backup，或已显式登记）
        if "backup" in root.name.lower() or root in SNAPSHOT_CONTAINER_ROOTS:
            target_dirs.add(root)
        # (b) root 下名字带演练/部署前缀的子目录
        for child in root.iterdir():
            if not child.is_dir() or child.name in skip_names:
                continue
            if child.name.startswith(SNAPSHOT_DIR_PREFIXES):
                target_dirs.add(child)
                # (c) 再下探一层（如 repair-deploy-*/rehearsal/）
                for grand in child.iterdir():
                    if grand.is_dir() and grand.name not in skip_names:
                        target_dirs.add(grand)

    for directory in sorted(target_dirs):
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            if any(ident in never for ident in _identities(candidate)):
                continue
            if not _is_snapshot_name(candidate.name):
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            item = Snapshot(candidate, size, directory)
            # 保全登记优先于目录名启发式：登记目录的整棵子树都不回收
            (guarded if is_protected(candidate, protected) else found).append(item)
    return found, guarded


def plan_reclaim(
    snapshots: list[Snapshot], *, keep: int, full_db_bytes: int = 1024**3
) -> tuple[list[Snapshot], list[Snapshot]]:
    """按目录聚合，保留最近 keep 个目录，其余目录内的快照标记为可回收。

    保留策略优先选择**含全库快照**（>= full_db_bytes）的目录作回滚点：
    只含 subset 快照的目录（几百 MB）作为回滚点没有意义，
    不应因为它日期最新就占用唯一的保留名额。
    """
    by_dir: dict[Path, int] = {}
    newest: dict[Path, float] = {}
    for item in snapshots:
        by_dir[item.directory] = max(by_dir.get(item.directory, 0), item.size)
        try:
            mtime = item.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        newest[item.directory] = max(newest.get(item.directory, 0.0), mtime)
    # 按目录内最新文件的 mtime 排序 —— 目录名（repair-deploy-* / repair-rehearsal-*）
    # 的字面序不反映真实时间先后，首版按名字排序会选错回滚点。
    ordered = sorted(by_dir, key=lambda p: newest.get(p, 0.0), reverse=True)
    full_dirs = [d for d in ordered if by_dir[d] >= full_db_bytes]
    candidates = full_dirs or ordered      # 没有全库快照时才退回任意目录
    keep_dirs = set(candidates[: max(keep, 0)])
    reclaim, retain = [], []
    for item in snapshots:
        (retain if item.directory in keep_dirs else reclaim).append(item)
    # 只回收够大的快照主文件所在目录里的项
    big_dirs = {i.directory for i in snapshots if i.size >= MIN_SNAPSHOT_BYTES}
    reclaim = [i for i in reclaim if i.directory in big_dirs]
    return reclaim, retain


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def report_snapshots(
    reclaim: list[Snapshot], retain: list[Snapshot], guarded: list[Snapshot] | None = None
) -> None:
    total = sum(i.size for i in reclaim)
    print(f"演练快照清单（扫描 {OUTPUTS}）")
    print(f"  可回收: {len(reclaim)} 个文件，合计 {human(total)}")
    print(f"  保留  : {len(retain)} 个文件，合计 {human(sum(i.size for i in retain))}")
    guarded = guarded or []
    if guarded:
        print(
            f"  受保护: {len(guarded)} 个文件，合计 {human(sum(i.size for i in guarded))}"
            f"（受控发布/回滚证据，绝不回收）"
        )
    print()
    if reclaim:
        print("可回收明细（前 15 行）：")
        for item in sorted(reclaim, key=lambda i: i.size, reverse=True)[:15]:
            print(f"  {human(item.size):>12}  {item.path.relative_to(REPO)}")
    if retain:
        print()
        print("保留（回滚点）：")
        for item in retain[:5]:
            print(f"  {human(item.size):>12}  {item.path.relative_to(REPO)}")
    if guarded:
        print()
        print("受保护（不回收）：")
        for item in sorted(guarded, key=lambda i: i.size, reverse=True):
            print(f"  {human(item.size):>12}  {item.path.relative_to(REPO)}")


def report_logs() -> list[tuple[Path, int]]:
    print()
    print(f"运行日志（扫描 {LOGS}）")
    rows: list[tuple[Path, int]] = []
    if not LOGS.is_dir():
        print("  （logs 目录不存在）")
        return rows
    for path in sorted(LOGS.glob("*.log")):
        if path.is_file():
            rows.append((path, path.stat().st_size))
    for path, size in sorted(rows, key=lambda r: -r[1])[:8]:
        print(f"  {human(size):>12}  {path.relative_to(REPO)}")
    if rows:
        print(f"  合计 {human(sum(s for _, s in rows))}")
    print()
    print("  说明：日志用 gzip 归档后截断（--rotate-logs），不删除历史归档。")
    return rows


def apply_reclaim(reclaim: list[Snapshot]) -> int:
    freed = 0
    for item in reclaim:
        try:
            size = item.size
            item.path.unlink()
            freed += size
            print(f"  已删除 {item.path.relative_to(REPO)}  ({human(size)})")
        except OSError as exc:
            print(f"  删除失败 {item.path}: {exc}", file=sys.stderr)
    # 清理空目录
    for directory in {i.directory for i in reclaim}:
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                print(f"  已移除空目录 {directory.relative_to(REPO)}")
        except OSError:
            pass
    return freed


def rotate_logs(rows: list[tuple[Path, int]], *, min_bytes: int) -> int:
    rotated = 0
    for path, size in rows:
        if size < min_bytes or path.name.endswith(".gz"):
            continue
        stamp = path.stat().st_mtime
        from datetime import datetime
        tag = datetime.fromtimestamp(stamp).strftime("%Y%m%d")
        target = path.with_name(f"{path.name}.{tag}.gz")
        index = 1
        while target.exists():
            target = path.with_name(f"{path.name}.{tag}.{index}.gz")
            index += 1
        try:
            with path.open("rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            path.write_bytes(b"")   # 截断，保持 fd 有效的写入者不受影响
            rotated += 1
            print(f"  已归档 {path.relative_to(REPO)} → {target.name} ({human(size)})")
        except OSError as exc:
            print(f"  归档失败 {path}: {exc}", file=sys.stderr)
    return rotated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claw 存储治理（演练快照回收 + 日志轮转）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只读审计）")
    parser.add_argument("--i-confirm-backup", action="store_true",
                        help="确认已知悉风险；--apply 时必须显式提供")
    parser.add_argument("--keep", type=int, default=1, help="保留最近 N 个快照目录作回滚点")
    parser.add_argument("--rotate-logs", action="store_true", help="同时归档并截断日志")
    parser.add_argument("--log-min-mb", type=int, default=100, help="日志归档体积阈值(MB)")
    args = parser.parse_args(argv)

    snapshots = find_snapshots()
    reclaim, retain = plan_reclaim(snapshots[0], keep=args.keep)
    guarded = snapshots[1]
    # 二次校验：受保护项绝不允许出现在可回收清单里（防止 plan_reclaim 回填）
    protected = load_protected()
    leaked = [i for i in reclaim if is_protected(i.path, protected)]
    if leaked:
        print("拒绝执行：可回收清单里出现受保护资产：", file=sys.stderr)
        for item in leaked:
            print(f"  {item.path}", file=sys.stderr)
        return 1
    report_snapshots(reclaim, retain, guarded)
    log_rows = report_logs()

    total = sum(i.size for i in reclaim)
    print()
    print(f"本次可释放：{human(total)}")

    if not args.apply:
        print("（只读审计模式，未删除任何文件）")
        print("确认后再执行：--apply --i-confirm-backup [--rotate-logs]")
        return 0
    if not args.i_confirm_backup:
        print("拒绝执行：--apply 需同时提供 --i-confirm-backup。", file=sys.stderr)
        return 1

    print()
    print("开始回收…")
    freed = apply_reclaim(reclaim)
    if args.rotate_logs:
        print()
        print("开始日志轮转…")
        rotate_logs(log_rows, min_bytes=args.log_min_mb * 1024 * 1024)
    print()
    print(f"完成：释放 {human(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

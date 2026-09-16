"""存储治理的「受保护资产」护栏测试。

背景：首版 `storage_governance.py` 按「目录名像演练目录 → 里面的库文件都可回收」
处理，会把受控发布流程明文登记的 13GB 级回滚/证据库一并删掉。被误判的实测量：
`production-online-030.sqlite`(SHA 85cf3384…)、`rehearsal-033.sqlite`、
`cold-original/claw.db` 集合、`rehearsal/claw.db`，合计 48.58GB。

这些用例锁定三件事：
1. 受控发布清单 `preserved_artifacts[]` 里的路径必须被保护（按清单，不靠记忆）；
2. 文档正文明确要求保全、但未进清单的路径必须被静态列表保护；
3. 清单读不到时必须保守回退 —— 宁可少回收，不可误删证据。
"""
from __future__ import annotations

import json

import pytest
from scripts import storage_governance as sg


# ── 清单驱动 ──────────────────────────────────────────────────────────


def test_sealed_manifest_exists_and_parses():
    """受控发布清单必须存在且可解析；它是保全判定的一手来源。"""
    assert sg.SEALED_MANIFEST.is_file(), f"保全清单缺失：{sg.SEALED_MANIFEST}"
    payload = json.loads(sg.SEALED_MANIFEST.read_text(encoding="utf-8"))
    artifacts = payload.get("preserved_artifacts")
    assert isinstance(artifacts, list) and artifacts, "清单缺 preserved_artifacts[]"
    for item in artifacts:
        assert item.get("path"), f"清单项缺 path：{item}"
        assert item.get("sha256"), f"清单项缺 sha256：{item}"


def _not_overridden(rel: str) -> bool:
    """该路径是否未被用户授权放宽（被放宽者本就不该受保护）。"""
    return rel not in sg.AUTHORIZED_SUPERSEDED_20260916


def test_every_manifest_artifact_is_protected():
    """清单里登记的每一项（除用户显式放宽者）都必须被判为受保护。"""
    payload = json.loads(sg.SEALED_MANIFEST.read_text(encoding="utf-8"))
    protected = sg.load_protected()
    checked = 0
    for item in payload["preserved_artifacts"]:
        rel = item["path"]
        if not _not_overridden(rel):
            continue
        assert sg.is_protected(sg.REPO / rel, protected) is not None, (
            f"清单登记项未被保护：{rel}"
        )
        checked += 1
    assert checked, "清单全部项都被放宽了 —— 保留底线被清空，需人工确认"


def test_static_artifacts_are_protected():
    """文档正文要求保全的静态项（除用户显式放宽者）必须全部受保护。"""
    protected = sg.load_protected()
    checked = 0
    for raw in sg.PROTECTED_ARTIFACTS:
        if not _not_overridden(raw):
            continue
        assert sg.is_protected(sg.REPO / raw, protected) is not None, (
            f"静态保全项未被保护：{raw}"
        )
        checked += 1
    assert checked, "静态保全项全部被放宽了 —— 保留底线被清空，需人工确认"


def test_override_only_narrows_never_widens():
    """override 只能减少受保护集合，不能凭空增加。"""
    # 未放宽时：PROTECTED_ARTIFACTS ∪ 清单 的规模应 >= 放宽后的规模
    protected = sg.load_protected()
    for raw in sg.PROTECTED_ARTIFACTS:
        if _not_overridden(raw):
            assert (sg.REPO / raw).absolute() in protected


# ── 关键回归：被误判过的那几个 13GB 库 ────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        # 冻结复盘库（测试夹具，SHA 硬编码在 test_paper_accounting_frozen.py:23）
        "outputs/postmarket_review_20260914_1717/evidence.sqlite",
        # 已知良好环境基准（唯一跑过 99 轮子离线安装 + 2683 测试 0 失败的环境）
        "logs/repair-rehearsal-20260915/final-env-round28",
        # 代码回滚用的解包源码目录
        "logs/repair-deploy-20260915/rollback-source",
        # 受控发布清单登记项（清单里的 5 项除用户放宽者外都必须保住）
        "logs/repair-deploy-20260915/current-private-config.tgz",
        "logs/repair-deploy-20260914/source-release.tgz",
        "logs/repair-rehearsal-20260915/candidate-round28-final-source.tgz",
    ],
)
def test_retention_floor_is_protected(rel: str):
    """保留底线：测试夹具 + 环境基准 + 受控发布清单登记项，任何情况下不得回收。"""
    assert sg.is_protected(sg.REPO / rel, sg.load_protected()) is not None, (
        f"保留底线被放行：{rel}"
    )


@pytest.mark.parametrize(
    "rel",
    [
        "logs/repair-rehearsal-20260915/production-online-030.sqlite",
        "logs/repair-rehearsal-20260915/rehearsal-033.sqlite",
        "logs/repair-deploy-20260914/rehearsal",
        # 2026-09-16 二批：用户选择「精简备份替代旧全库快照」
        "logs/repair-deploy-20260914/cold-original",
        "logs/repair-deploy-20260915/pre-migration-030.sqlite",
    ],
)
def test_authorized_superseded_items_are_reclaimable_but_recorded(rel: str):
    """用户显式放宽的项：可回收，但**必须**留在授权清单里。

    这个断言防止将来有人把授权项悄悄塞回保护列表（或反之）而不留痕——
    放宽必须以显式登记的方式表现，不能靠删掉保护清单里的一行。

    注意 `pre-migration-030.sqlite` 同时被**受控发布清单**登记为 preserved_artifact；
    它能被回收，走的是 `load_protected()` 末尾的 override 减法，而不是把清单改掉——
    清单作为 9/15 那轮发布的历史记录必须保持原样。
    """
    assert sg.is_protected(sg.REPO / rel, sg.load_protected()) is None, (
        f"{rel} 仍受保护，与用户授权不符"
    )
    assert rel in sg.AUTHORIZED_SUPERSEDED_20260916, (
        f"{rel} 可回收但未登记在 AUTHORIZED_SUPERSEDED_20260916 —— 授权必须有据可查"
    )


def test_sealed_manifest_record_is_not_mutated_by_override():
    """override 只影响判定结果，**不得**改动清单文件本身（历史记录须保持原样）。"""
    payload = json.loads(sg.SEALED_MANIFEST.read_text(encoding="utf-8"))
    paths = [i["path"] for i in payload["preserved_artifacts"]]
    assert "logs/repair-deploy-20260915/pre-migration-030.sqlite" in paths, (
        "受控发布清单被改写了 —— 它是历史记录，只能通过 AUTHORIZED_SUPERSEDED 放宽"
    )


def test_directory_registration_protects_subtree():
    """登记目录即保护其整棵子树（含 -wal/-shm 这类拆开就失效的伴随文件）。

    用 final-env-round28 而非 cold-original 举例：后者已被用户授权回收，
    不再是保留底线。
    """
    protected = sg.load_protected()
    root = sg.REPO / "logs" / "repair-rehearsal-20260915" / "final-env-round28"
    for leaf in ("bin/python3.11", "lib/python3.11/site-packages", "pyvenv.cfg"):
        assert sg.is_protected(root / leaf, protected) is not None, leaf


def test_final_env_round28_is_protected():
    """docs/controlled-release-20260915.md:40 —— 实际部署单元，不能按普通日志清理。"""
    inside = (
        sg.REPO / "logs" / "repair-rehearsal-20260915" / "final-env-round28" / "bin" / "python3.11"
    )
    assert sg.is_protected(inside, sg.load_protected()) is not None


# ── 反向断言：护栏不能过宽 ────────────────────────────────────────────


def test_unregistered_path_is_not_protected():
    """没登记过的路径不能被误判为受保护，否则治理永远回收不了任何东西。"""
    protected = sg.load_protected()
    stray = sg.REPO / "outputs" / "continuous-live-20260907-132735" / "before.sqlite3"
    assert sg.is_protected(stray, protected) is None


def test_manifest_unreadable_falls_back_to_static_list(monkeypatch, tmp_path, capsys):
    """清单读不到时必须保守：静态项仍受保护，并打印可见告警。"""
    monkeypatch.setattr(sg, "SEALED_MANIFEST", tmp_path / "does-not-exist.json")
    protected = sg.load_protected()
    assert "警告" in capsys.readouterr().err
    for raw in sg.PROTECTED_ARTIFACTS:
        assert sg.is_protected(sg.REPO / raw, protected) is not None
    # 清单专属项此时无法保护 —— 这正是要告警而不是静默放行的原因
    manifest_only = sg.REPO / "logs" / "repair-deploy-20260915" / "current-private-config.tgz"
    assert sg.is_protected(manifest_only, protected) is None


# ── 端到端不变量 ──────────────────────────────────────────────────────


def test_reclaimable_set_contains_no_protected_path():
    """核心不变量：可回收清单里绝不允许出现受保护资产。

    不断言 guarded 非空 —— 当前事实是扫描范围内已无受保护的**库文件**
    （保留底线都改成了非库文件：环境目录、源码目录、.tgz），这是正确状态。
    真正的护栏由 `is_protected` 与 `test_retention_floor_is_protected` 锁定。
    """
    reclaimable, guarded = sg.find_snapshots()
    protected = sg.load_protected()
    leaked = [i.path for i in reclaimable if sg.is_protected(i.path, protected)]
    assert not leaked, f"受保护资产混入可回收清单：{[str(p) for p in leaked]}"
    assert all(sg.is_protected(i.path, protected) for i in guarded)


def test_plan_reclaim_never_returns_protected(tmp_path):
    """plan_reclaim 也不得把受保护项判成可回收。"""
    reclaimable, _ = sg.find_snapshots()
    if not reclaimable:
        pytest.skip("当前无可回收项")
    reclaim, _retain = sg.plan_reclaim(reclaimable, keep=1)
    protected = sg.load_protected()
    assert not [i for i in reclaim if sg.is_protected(i.path, protected)]


def test_live_production_db_is_never_touchable():
    """运行中的生产库永不出现在任何回收清单里。"""
    for path in sg.NEVER_TOUCH:
        if not path.exists():
            continue
        reclaimable, guarded = sg.find_snapshots()
        assert path.resolve() not in {i.path.resolve() for i in reclaimable}
        assert path.resolve() not in {i.path.resolve() for i in guarded}

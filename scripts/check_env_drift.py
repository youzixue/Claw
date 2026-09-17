"""依赖漂移检查：把"安装环境 vs 声明环境"的差异变成一条命令。

为什么需要它
------------
2026-09-15 的部署走查只构建了**声明环境**（`logs/repair-rehearsal-20260915/
declared-env-round28`，按 `backend/requirements.txt` 逐个钉版安装），
但后端实际一直跑在系统级 Python 上。9/16 复查发现运行时环境与受控环境
**有 14 个包不一致**（fastapi 0.115.0→0.111.1、sqlalchemy 2.0.35→2.0.48、
pandas 2.2.3→2.3.3、numpy 2.1.1→1.26.4、httpx 0.27.2→0.25.2 等），
也就是说"测过的环境"和"跑着的环境"不是同一个 —— 这是隐性风险，
此前只能靠人工比对发现。

用法
----
    python -m scripts.check_env_drift               # 运行时依赖
    python -m scripts.check_env_drift --dev         # 连同开发/测试工具链

检查的是**当前解释器**的环境；要检查受控 venv，用那个 venv 的 python 运行本脚本：
    ../logs/repair-rehearsal-20260915/declared-env-round28/bin/python \
        ../scripts/check_env_drift.py --dev

退出码
------
0 = 无漂移；1 = 有漂移或声明文件不可解析。
本脚本**只读**：不安装、不升级、不改任何文件。
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"


def parse_requirements(path: Path) -> dict[str, str]:
    pinned: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name = line.split("==", 1)[0].split("[", 1)[0].strip()
        pinned[name] = line.split("==", 1)[1].strip()
    return pinned


def installed(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "MISSING"


def check(declared: dict[str, str], *, dev: bool, quiet: bool) -> list[tuple[str, str, str]]:
    drifted = []
    width = max((len(n) for n in declared), default=10) + 2
    if not quiet:
        print(f"{'package'.ljust(width)}{'pinned'.ljust(14)}{'installed'.ljust(14)}status")
    for name, want in sorted(declared.items()):
        have = installed(name)
        ok = have == want
        if not ok:
            drifted.append((name, want, have))
        if not quiet:
            print(f"{name.ljust(width)}{want.ljust(14)}{have.ljust(14)}{'OK' if ok else 'DRIFT'}")
    if dev:
        dev_path = BACKEND / "requirements-dev.txt"
        if dev_path.is_file():
            dev_declared = parse_requirements(dev_path)
            for name, want in sorted(dev_declared.items()):
                have = installed(name)
                ok = have == want
                if not ok:
                    drifted.append((f"{name} (dev)", want, have))
                if not quiet:
                    print(f"{name.ljust(width)}{want.ljust(14)}{have.ljust(14)}"
                          f"{'OK' if ok else 'DRIFT'} (dev)")
    return drifted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖漂移检查（只读）")
    parser.add_argument("--dev", action="store_true", help="连同 requirements-dev.txt 一起检查")
    parser.add_argument("--quiet", action="store_true", help="只输出漂移汇总")
    args = parser.parse_args(argv)

    runtime = BACKEND / "requirements.txt"
    if not runtime.is_file():
        print(f"找不到 {runtime}", file=sys.stderr)
        return 1

    declared = parse_requirements(runtime)
    if not declared:
        print(f"{runtime} 没有可解析的钉版条目", file=sys.stderr)
        return 1

    print(f"解释器: {sys.executable}")
    print(f"声明文件: {runtime}")
    drifted = check(declared, dev=args.dev, quiet=args.quiet)
    print()
    if not drifted:
        print("无漂移：安装环境与声明环境一致")
        return 0
    print(f"发现 {len(drifted)} 项漂移（安装环境 ≠ 声明环境）:")
    for name, want, have in drifted:
        print(f"  - {name}: 声明 {want} / 实际 {have}")
    print()
    print("说明：漂移本身不等于缺陷，但'测过的环境'与'跑着的环境'不同会让")
    print("测试结论失去可迁移性。处理方式见 backend/requirements.txt 相邻的")
    print("环境说明与 outputs/ 下的环境漂移结论文档。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""追高过滤阈值校准 CLI（只读分析，不改库）

核心逻辑已收敛到 app.signal.chase_calibration.build_chase_calibration_report，
本脚本仅作为手动跑完整阈值扫查表的入口，并打印完整报告。

用法:
  python3 scripts/calibrate_chase_thresholds.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.signal.chase_calibration import build_chase_calibration_report


async def main() -> None:
    report = await build_chase_calibration_report()
    print(report["detail"])


if __name__ == "__main__":
    asyncio.run(main())
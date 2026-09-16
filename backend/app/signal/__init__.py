"""牛股预测引擎 — 6 子模块

1. 龙头股识别 (dragon_head)
2. 连板股追踪 (limit_up_tracker)
3. 资金异动监测 (capital_anomaly)
4. 筹码集中度 (chip_concentration)
5. 突破信号检测 (breakthrough)
6. 综合评分模型 (bull_score)

纯因子+规则，不依赖 AI
"""

from app.signal.dragon_head import DragonHeadScanner
from app.signal.limit_up_tracker import LimitUpTracker
from app.signal.capital_anomaly import CapitalAnomalyDetector
from app.signal.chip_concentration import ChipConcentrationAnalyzer
from app.signal.breakthrough import BreakthroughDetector
from app.signal.bull_score import BullScoreModel

__all__ = [
    "DragonHeadScanner",
    "LimitUpTracker",
    "CapitalAnomalyDetector",
    "ChipConcentrationAnalyzer",
    "BreakthroughDetector",
    "BullScoreModel",
]

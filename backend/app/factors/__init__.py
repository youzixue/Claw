"""因子引擎 — 10类48因子

分类:
  technical(8)   技术因子: ma5_bias/ma20_bias/macd_signal/rsi_14/kdj_golden/boll_position/volume_ratio/turnover_rate
  fund_flow(5)   资金因子: main_inflow_strength/big_order_pct/fund_flow_trend_3d/retail_outflow_pct/sector_fund_flow
  sentiment(5)   情绪因子: sentiment_cycle/limit_up_count/seal_rate/board_height/advance_decline_ratio
  sector(5)      板块因子: sector_strength/sector_persistence/sector_leader/sector_resonance/sector_rotation
  fundamental(5) 基本面:   pe_pct/pb_pct/roe/revenue_growth/dividend_yield
  breakout(5)    爆发因子: volume_spike/fund_concentration/seal_strength/first_board_quality/breakout_energy
  promotion(5)   晋级因子: promotion_rate/sector_support/historical_promotion/auction_strength/chip_structure
  lifecycle(5)   生命周期: lifecycle_stage/acceleration_signal/divergence_degree/topping_warning/lifecycle_score
  news(3)        新闻因子: news_heat/bull_resonance/policy_sensitivity
  margin(2)      融资融券: margin_buy_strength/margin_balance_trend
"""

# 导入基类(必须在因子之前)
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, FactorEngine, factor_engine

# 导入所有因子模块(触发注册)
from app.factors.technical import *    # 8个
from app.factors.fund_flow import *    # 5个
from app.factors.sentiment import *    # 5个
from app.factors.sector import *       # 5个
from app.factors.fundamental import *  # 5个
from app.factors.breakout import *     # 5个
from app.factors.promotion import *    # 5个
from app.factors.lifecycle import *    # 5个
from app.factors.news import *         # 3个
from app.factors.margin import *       # 2个

# 合计: 48个因子

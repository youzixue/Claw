from pydantic import BaseModel
from typing import List, Optional


class ExternalFactorItem(BaseModel):
    key: str
    label: str
    price: float = 0
    change_pct: float = 0
    trade_time: Optional[str] = None
    market: Optional[str] = None


class MappingInsightItem(BaseModel):
    source_key: str
    source_label: str
    a_share_themes: List[str]
    status: str = "pending"  # synced/diverging/lagging/pending
    note: Optional[str] = None


class OverviewConclusionItem(BaseModel):
    key: str
    label: str
    value: str
    tone: str = "neutral"  # positive/negative/warning/neutral
    note: Optional[str] = None


class AShareIndexItem(BaseModel):
    code: str
    label: str
    price: float = 0
    change_pct: float = 0


class AShareCoreState(BaseModel):
    indices: List[AShareIndexItem] = []
    sentiment_cycle: Optional[str] = None
    limit_up_count: int = 0
    limit_down_count: int = 0
    seal_rate: float = 0
    board_height: int = 0
    main_net_inflow: float = 0


class Dashboard2Snapshot(BaseModel):
    trade_date: Optional[str] = None
    snapshot_time: Optional[str] = None
    summary_text: Optional[str] = None
    conclusions: List[OverviewConclusionItem] = []
    a_share_core: Optional[AShareCoreState] = None
    external_factors: List[ExternalFactorItem] = []
    mapping_insights: List[MappingInsightItem] = []

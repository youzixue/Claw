"""Dashboard 2.0 聚合服务"""

from datetime import datetime
from .mapping import GLOBAL_TO_CN_SECTOR_MAPPING
from .schemas import Dashboard2Snapshot, MappingInsightItem
from .external_sources import external_factor_collector


class Dashboard2Service:
    def build_snapshot(self) -> Dashboard2Snapshot:
        factors = external_factor_collector.collect()
        factor_keys = {f.key for f in factors}
        mappings = [
            MappingInsightItem(
                source_key=k,
                source_label=v["label"],
                a_share_themes=v["a_share_themes"],
                status="pending" if k in factor_keys else "missing",
                note="已接入实时外部因子" if k in factor_keys else "数据源待接入",
            )
            for k, v in GLOBAL_TO_CN_SECTOR_MAPPING.items()
        ]
        return Dashboard2Snapshot(
            trade_date=None,
            snapshot_time=datetime.now().isoformat(timespec="seconds"),
            external_factors=factors,
            mapping_insights=mappings,
        )


dashboard2_service = Dashboard2Service()

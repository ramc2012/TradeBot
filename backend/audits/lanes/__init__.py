from audits.lanes.auction_intelligence import AuctionIntelligenceAuditor
from audits.lanes.commodity_mp_orderflow import CommodityMpOrderFlowAuditor
from audits.lanes.directional_options import DirectionalOptionsAuditor
from audits.lanes.institutional_convergence import InstitutionalConvergenceAuditor
from audits.lanes.macd_refined import MacdRefinedAuditor
from audits.lanes.s1_atm_30m_macd import S1Auditor

# Registry keys are the auditor selection keys (CLI `--lane <key>`) AND the value
# a LaneSpec.audit_lane_key must carry for core.lane_registry to flip
# audit_coverage=True. Keys here match the lane keys in core/lane_registry.py so
# the wiring is audit_lane_key == lane key for every execution-capable lane.
#
# s1 keeps its historical short key ("s1"; lane key "s1_atm_30m_macd"). The five
# execution-capable lanes added below (2026-07-18) close the audit-coverage gate
# the review flagged before the process split. Parked lanes (chain_candle_builder;
# s2_index_mp_macd and us_macd_refined were RETIRED 2026-07-20) and pure daemons/monitors are
# intentionally excluded.
REGISTRY = {
    "s1": S1Auditor,
    "directional_options": DirectionalOptionsAuditor,
    "macd_refined": MacdRefinedAuditor,
    "auction_intelligence": AuctionIntelligenceAuditor,
    "institutional_convergence": InstitutionalConvergenceAuditor,
    "commodity_mp_orderflow": CommodityMpOrderFlowAuditor,
}

from nomad_sniper.labels.actual_trades import label_actual_trades
from nomad_sniper.labels.breakeven import BreakevenResult, calibrate_m_breakeven
from nomad_sniper.labels.cost_model import CostModel, ZerodhaFnoCostModel
from nomad_sniper.labels.directional import (
    DirectionalLabel,
    build_labels_for_grid,
    label_grid_point,
)
from nomad_sniper.labels.profitability_gate import (
    ActualOptionGate,
    AtrProxyGate,
    BsProxyGate,
    GateContext,
    ProfitabilityGate,
    make_gate,
)
from nomad_sniper.labels.triple_barrier import TripleBarrierLabel, label_triple_barrier

__all__ = [
    "CostModel",
    "ZerodhaFnoCostModel",
    "label_actual_trades",  # validation overlay only (contract §7)
    "TripleBarrierLabel",
    "label_triple_barrier",
    "ProfitabilityGate",
    "AtrProxyGate",
    "BsProxyGate",
    "ActualOptionGate",
    "GateContext",
    "make_gate",
    "DirectionalLabel",
    "label_grid_point",
    "build_labels_for_grid",
    "calibrate_m_breakeven",
    "BreakevenResult",
]

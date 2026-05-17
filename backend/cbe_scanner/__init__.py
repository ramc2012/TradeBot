"""CBE Scanner - Compression-Before-Expansion signal generator for NSE F&O."""

from .features import (
    CBEConfig, CBEScore,
    feature_volatility_compression,
    feature_option_positioning,
    feature_cross_sectional_divergence,
    feature_catalyst_proximity,
    feature_microstructure_pressure,
    compute_cbe_score,
    scan_universe,
    generate_watchlist,
)
from .data_provider import DataProvider, SyntheticDataProvider

__version__ = "0.1.0"

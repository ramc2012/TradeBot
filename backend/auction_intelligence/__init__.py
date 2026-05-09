"""Auction-intelligence strategy package.

This package remains isolated from the legacy strategy runtime. Paper-mode
automation is coordinated by the market-hours supervisor instead of the API
router layer.
"""

from auction_intelligence.service import AuctionIntelligenceService

__all__ = ["AuctionIntelligenceService"]

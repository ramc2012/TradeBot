"""Auction-intelligence strategy package.

This package is intentionally isolated from the existing strategy runtime.
Nothing here auto-starts on application boot.
"""

from auction_intelligence.service import AuctionIntelligenceService

__all__ = ["AuctionIntelligenceService"]

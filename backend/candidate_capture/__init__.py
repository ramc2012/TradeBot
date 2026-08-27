"""Candidate capture — a read-only observer that records EVERY evaluated
option contract, not only the ones some lane chose to trade.

SAFETY BOUNDARY. Nothing in this package may ever place an order, paper or
live. There is no central `allow_live_orders` kill switch in this codebase —
routing is structural, decided by which code path a caller invokes — so the
guarantee here is the import list itself: no module in `candidate_capture`
imports `live_engine.*`, `paper_engine.*`, `api.routers.trading` or any
`brokers.*` order-submission function. `tests/test_candidate_capture_safety.py`
asserts that and will fail the suite if it ever stops being true.
"""

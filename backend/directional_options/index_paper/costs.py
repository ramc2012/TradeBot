"""Fill modelling: spread, impact and statutory charges.

A long-premium lane booked at the mid is not a paper trade.  The directional
research in this stack already showed the difference is decisive — an entry
edge that measured positive turned negative under a single bar of fill lag,
and the option-explosion study found that at a +25/-30 payoff you need 54.5%
accuracy just to break even.  Cost has to be inside the fill, not deducted in
a report afterwards.

An honest statement of what is modelled and what is assumed:

  SPREAD is ASSUMED, not measured.  `option_premium_candles` stores OHLC,
  volume, OI, IV and greeks — there is no bid/ask anywhere in it, so there is
  nothing to calibrate against.  The half-spread here is a documented PRIOR in
  ticks, widened by illiquidity and by distance from the money, and it is
  almost certainly the largest single source of error in this lane's P&L.
  `calibrate_half_spread_ticks` is the seam where real fills replace the prior;
  until it is fed, treat lane P&L as an upper bound.

  IMPACT is modelled with the square-root law, impact = eta * sigma *
  sqrt(Q / ADV).  At paper size on index options it is small, but it is the
  term that decides whether a strategy has capacity, so it is here from the
  start rather than added when size grows.

  CHARGES are the Indian F&O schedule.  Rates change by circular; they are
  parameters, and the defaults carry the date they were set.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

Intent = Literal["entry", "exit"]
Side = Literal["buy", "sell"]

TICK_SIZE = 0.05
TRADING_DAYS_PER_YEAR = 252.0


@dataclass(frozen=True)
class FeeSchedule:
    """Indian index-option charges. Defaults as of 2026-09; verify per broker.

    STT applies to the SELL leg of an option trade on premium; stamp duty
    applies to the BUY leg.  GST is charged on brokerage plus exchange and
    regulator fees, never on STT or stamp duty.
    """

    brokerage_per_order: float = 20.0
    stt_sell_rate: float = 0.0010          # 0.10% of premium turnover, sell side
    exchange_txn_rate: float = 0.0003503   # NSE options, on premium turnover
    sebi_turnover_rate: float = 0.000001   # Rs 10 per crore
    stamp_duty_buy_rate: float = 0.00003   # 0.003% of premium turnover, buy side
    gst_rate: float = 0.18

    def charges(self, premium: float, quantity: int, side: Side) -> dict[str, float]:
        turnover = max(float(premium), 0.0) * max(int(quantity), 0)
        brokerage = self.brokerage_per_order
        exchange = turnover * self.exchange_txn_rate
        sebi = turnover * self.sebi_turnover_rate
        stt = turnover * self.stt_sell_rate if side == "sell" else 0.0
        stamp = turnover * self.stamp_duty_buy_rate if side == "buy" else 0.0
        gst = (brokerage + exchange + sebi) * self.gst_rate
        total = brokerage + exchange + sebi + stt + stamp + gst
        return {
            "turnover": turnover,
            "brokerage": brokerage,
            "exchange": exchange,
            "sebi": sebi,
            "stt": stt,
            "stamp_duty": stamp,
            "gst": gst,
            "total": total,
        }


@dataclass(frozen=True)
class CostModel:
    """Spread and impact assumptions, with the calibration seam attached."""

    fees: FeeSchedule = field(default_factory=FeeSchedule)
    tick_size: float = TICK_SIZE

    # Half-spread in ticks by liquidity bucket.  A PRIOR — see module docstring.
    #
    # Calibrated by judgement against observed Indian index-option quoting, not
    # against data, because this feed has none: a NIFTY weekly ATM tends to
    # quote 0.10-0.50 wide, a SENSEX weekly 0.50-2.00, a BANKNIFTY monthly
    # 1.00-3.00.  The values below sit at the WIDE end of those ranges on
    # purpose — an optimistic spread makes a losing lane look profitable, and
    # this is the single largest modelling uncertainty in the book.
    #
    # `spread_multiplier` exists to run the sensitivity.  Any conclusion drawn
    # from this lane's P&L should be re-checked at 2.0x and 3.0x before it is
    # believed; if the sign of the result changes, the result is about the
    # spread assumption and not about the strategy.
    half_spread_ticks_liquid: float = 3.0
    half_spread_ticks_normal: float = 10.0
    half_spread_ticks_thin: float = 30.0
    spread_multiplier: float = 1.0

    # Volume thresholds (contracts traded in the bar) separating the buckets.
    liquid_volume: float = 50_000.0
    normal_volume: float = 5_000.0

    # VOLUME IS NOT AVAILABLE in this feed.  97.5% of index option rows in
    # `option_premium_candles` carry volume = 0 while open interest is
    # populated on essentially all of them — the chain endpoint simply does not
    # return traded volume.  Liquidity therefore falls back to OI, and any gate
    # written against volume is an unpassable veto, not a filter.
    liquid_oi: float = 500_000.0
    normal_oi: float = 50_000.0

    # Prior for turning OI into a daily-turnover proxy for the impact term.
    # Index weeklies churn a large fraction of their open interest each day;
    # 0.5 is a mid-range assumption and it only scales the impact term, which
    # is the smallest of the three cost components at paper size.
    oi_to_adv_ratio: float = 0.5

    # Extra half-spread ticks per unit of |log-moneyness| — wings are wider.
    wing_ticks_per_k: float = 40.0

    # Square-root impact coefficient.  eta ~ 0.5-1.0 is the range usually
    # reported for equity-like instruments; 0.6 is a mid-range prior.
    impact_eta: float = 0.6
    min_adv_contracts: float = 1_000.0

    # Marks and fills come off a 30-minute close.  Crossing the spread from a
    # bar close is optimistic by roughly the amount the tape moved inside the
    # bar, so a fixed adverse slippage in ticks is added on top.
    lag_slippage_ticks: float = 1.0

    def liquidity_bucket(self, volume: float | None, oi: float | None = None) -> str:
        """Volume when it exists, open interest when it does not.

        Falling back to OI rather than treating a zero-volume row as illiquid
        matters: with volume absent from every row, a volume-only bucket makes
        every contract "thin", which triples the assumed spread and quietly
        vetoes the whole universe through the cost gate.
        """
        v = float(volume or 0.0)
        if v > 0:
            if v >= self.liquid_volume:
                return "liquid"
            if v >= self.normal_volume:
                return "normal"
            return "thin"
        o = float(oi or 0.0)
        if o >= self.liquid_oi:
            return "liquid"
        if o >= self.normal_oi:
            return "normal"
        return "thin"

    def adv_proxy(self, volume: float | None, oi: float | None) -> float:
        v = float(volume or 0.0)
        if v > 0:
            return v
        return float(oi or 0.0) * self.oi_to_adv_ratio

    def half_spread(
        self,
        volume: float | None,
        log_moneyness: float | None,
        oi: float | None = None,
    ) -> float:
        bucket = self.liquidity_bucket(volume, oi)
        ticks = {
            "liquid": self.half_spread_ticks_liquid,
            "normal": self.half_spread_ticks_normal,
            "thin": self.half_spread_ticks_thin,
        }[bucket]
        ticks += self.wing_ticks_per_k * abs(float(log_moneyness or 0.0))
        return ticks * self.tick_size * max(self.spread_multiplier, 0.0)

    def impact(
        self,
        quantity: int,
        adv_contracts: float | None,
        reference_price: float,
        sigma: float | None,
    ) -> float:
        """Square-root impact in rupees per unit, not in fraction of price.

        `sigma` arrives ANNUALISED, as implied vol always does here, but the
        square-root law is conventionally stated in DAILY volatility — impact
        is a one-day phenomenon.  Feeding the annualised number straight in
        overstates impact by a factor of sqrt(252), roughly 16x.
        """
        adv = max(float(adv_contracts or 0.0), self.min_adv_contracts)
        participation = max(int(quantity), 0) / adv
        annual_vol = float(sigma) if sigma and sigma > 0 else 0.0
        if annual_vol <= 0 or participation <= 0:
            return 0.0
        daily_vol = annual_vol / math.sqrt(TRADING_DAYS_PER_YEAR)
        return self.impact_eta * daily_vol * math.sqrt(participation) * float(reference_price)


@dataclass(frozen=True)
class FillEstimate:
    reference_price: float
    fill_price: float
    side: Side
    intent: Intent
    quantity: int
    half_spread: float
    lag_slippage: float
    impact: float
    charges: dict[str, float]
    spread_vol_points: float | None
    liquidity_bucket: str

    @property
    def slippage_per_unit(self) -> float:
        return abs(self.fill_price - self.reference_price)

    @property
    def total_cost(self) -> float:
        """Every rupee the fill costs versus transacting at the reference."""
        return self.slippage_per_unit * self.quantity + self.charges["total"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "side": self.side,
            "intent": self.intent,
            "quantity": self.quantity,
            "half_spread": self.half_spread,
            "lag_slippage": self.lag_slippage,
            "impact": self.impact,
            "slippage_per_unit": self.slippage_per_unit,
            "charges": self.charges,
            "total_cost": self.total_cost,
            "spread_vol_points": self.spread_vol_points,
            "liquidity_bucket": self.liquidity_bucket,
        }


def estimate_fill(
    reference_price: float,
    quantity: int,
    side: Side,
    intent: Intent,
    *,
    model: CostModel | None = None,
    volume: float | None = None,
    oi: float | None = None,
    log_moneyness: float | None = None,
    adv_contracts: float | None = None,
    sigma: float | None = None,
    vega_per_vol_point: float | None = None,
) -> FillEstimate:
    """Price one paper fill, always adverse to the trader.

    A buy fills above the reference and a sell below it, by half-spread plus
    lag slippage plus impact.  There is no configuration in which this function
    returns a fill better than the reference, which is deliberate: the failure
    mode this lane must not have is a book that looks profitable because it
    transacted at prices nobody offered.
    """
    cost_model = model or CostModel()
    qty = max(int(quantity), 0)
    ref = max(float(reference_price), 0.0)

    half_spread = cost_model.half_spread(volume, log_moneyness, oi)
    lag = cost_model.lag_slippage_ticks * cost_model.tick_size
    adv = adv_contracts if adv_contracts else cost_model.adv_proxy(volume, oi)
    impact = cost_model.impact(qty, adv, ref, sigma)

    adverse = half_spread + lag + impact
    fill = ref + adverse if side == "buy" else max(ref - adverse, cost_model.tick_size)

    charges = cost_model.fees.charges(fill, qty, side)
    # Half-spread expressed in VOL POINTS: one vol point is a 0.01 change in
    # sigma, and `vega_per_vol_point` is already the price move for exactly
    # that, so the ratio is in vol points directly.  Naming and units are kept
    # deliberately aligned here — a percent-versus-fraction slip in this stack
    # once rendered a whole return series 100x too large.
    spread_vol_points = None
    if vega_per_vol_point and vega_per_vol_point > 0:
        spread_vol_points = half_spread / float(vega_per_vol_point)

    return FillEstimate(
        reference_price=ref,
        fill_price=fill,
        side=side,
        intent=intent,
        quantity=qty,
        half_spread=half_spread,
        lag_slippage=lag,
        impact=impact,
        charges=charges,
        spread_vol_points=spread_vol_points,
        liquidity_bucket=cost_model.liquidity_bucket(volume, oi),
    )


def round_trip_cost_premium_fraction(
    reference_price: float,
    quantity: int,
    *,
    model: CostModel | None = None,
    volume: float | None = None,
    oi: float | None = None,
    log_moneyness: float | None = None,
    adv_contracts: float | None = None,
    sigma: float | None = None,
) -> float | None:
    """What fraction of the premium a full round trip consumes.

    This is the number that decides whether a structure is tradeable at all.
    On a thin wing contract it routinely exceeds 10% of premium, which means a
    signal has to be right about a move larger than its own transaction cost
    before it earns anything — and that test belongs in front of the trade,
    not in the monthly review.
    """
    if reference_price <= 0 or quantity <= 0:
        return None
    entry = estimate_fill(
        reference_price, quantity, "buy", "entry",
        model=model, volume=volume, oi=oi, log_moneyness=log_moneyness,
        adv_contracts=adv_contracts, sigma=sigma,
    )
    exit_ = estimate_fill(
        reference_price, quantity, "sell", "exit",
        model=model, volume=volume, oi=oi, log_moneyness=log_moneyness,
        adv_contracts=adv_contracts, sigma=sigma,
    )
    total = entry.total_cost + exit_.total_cost
    return total / (reference_price * quantity)


def calibrate_half_spread_ticks(
    observed: Sequence[tuple[float, float, float]],
    tick_size: float = TICK_SIZE,
) -> dict[str, float] | None:
    """Replace the spread PRIOR with a measurement, once fills exist.

    `observed` is [(reference_price, fill_price, volume), ...] from real or
    broker-simulated executions.  Returns median half-spread in ticks per
    liquidity bucket, ready to be fed back into `CostModel`.  Returns None
    below 30 observations per bucket rather than fitting noise.
    """
    buckets: dict[str, list[float]] = {"liquid": [], "normal": [], "thin": []}
    model = CostModel(tick_size=tick_size)
    for reference, fill, volume in observed:
        if reference <= 0 or fill <= 0:
            continue
        buckets[model.liquidity_bucket(volume)].append(abs(fill - reference) / tick_size)

    out: dict[str, float] = {}
    for name, values in buckets.items():
        if len(values) < 30:
            continue
        values.sort()
        out[name] = values[len(values) // 2]
    return out or None

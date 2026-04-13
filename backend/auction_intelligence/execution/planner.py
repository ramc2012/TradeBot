from __future__ import annotations

from auction_intelligence.schemas import AgentDecision, ExecutionInstruction, OrderFlowSnapshot, SessionContext


class ExecutionPlanner:
    def plan(
        self,
        session: SessionContext,
        decision: AgentDecision,
        order_flow: OrderFlowSnapshot,
    ) -> ExecutionInstruction:
        if decision.action == "FLAT":
            return ExecutionInstruction(
                agent_name=decision.agent_name,
                symbol=session.symbol,
                action="FLAT",
                style="WAIT",
                order_type="NONE",
                limit_price=None,
                slices=0,
                cancel_after_seconds=0,
                rationale=["No executable action."],
                quantity=0,
                underlying_symbol=session.symbol,
                instrument_type="FUT",
            )

        style = order_flow.execution_aggression
        order_type = "LIMIT" if style == "PASSIVE" else "MARKET"
        limit_price = None
        if order_type == "LIMIT":
            limit_price = order_flow.mid_price if decision.action == "LONG" else order_flow.mid_price

        rationale = [
            f"Execution style chosen from order flow: {style.lower()}",
            f"Passive fill probability={order_flow.passive_fill_probability:.2f}",
            f"Aggressive fill probability={order_flow.aggressive_fill_probability:.2f}",
            f"Book pressure={order_flow.book_pressure:.2f}, toxicity={order_flow.toxicity_score:.2f}",
        ]
        return ExecutionInstruction(
            agent_name=decision.agent_name,
            symbol=session.symbol,
            action=decision.action,
            style=style,
            order_type=order_type,
            limit_price=round(limit_price, 4) if limit_price is not None else None,
            slices=2 if style == "PASSIVE" else 1,
            cancel_after_seconds=30 if style == "PASSIVE" else 5,
            rationale=rationale,
            quantity=int(decision.quantity or 0),
            broker_action="BUY" if decision.action == "LONG" else "SELL",
            underlying_symbol=session.symbol,
            instrument_type="FUT",
            decision_confidence=round(float(decision.confidence), 4),
        )

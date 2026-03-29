"use client";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  placeOrder,
  getOrders,
  cancelOrder,
  getPositions,
  getTrades,
  getStrategyAgentStatus,
} from "@/lib/api";
import {
  StrategyAgentStatus,
  StrategyMonitorSection,
  StrategyStatusPanel,
  formatSigned,
} from "@/components/trading/StrategyAgentMonitor";
import { useStore } from "@/store";
import { clsx } from "clsx";
import { Plus, X, Zap } from "lucide-react";

type OrderType = "MARKET" | "LIMIT" | "SL" | "SL_M";
type ActionType = "BUY" | "SELL";
type InstrumentType = "CE" | "PE" | "FUT" | "EQ";

type PortfolioPosition = {
  symbol: string;
  action: string;
  qty: number;
  avg_price: number;
  ltp: number;
  unrealized_pnl: number;
  instrument_type?: string;
  expiry?: string | null;
  strike?: number | null;
  option_type?: string | null;
};

type PortfolioTrade = {
  symbol: string;
  action: string;
  qty: number;
  entry_price?: number | null;
  exit_price?: number | null;
  pnl?: number | null;
  entry_time?: string | null;
  exit_time?: string | null;
};

// Quick strategy templates
const STRATEGIES = [
  {
    name: "Buy Straddle",
    fill: (atm: number) => [
      { action: "BUY", instrument_type: "CE", strike: atm, order_type: "MARKET", qty: 1 },
      { action: "BUY", instrument_type: "PE", strike: atm, order_type: "MARKET", qty: 1 },
    ],
  },
  {
    name: "Sell Strangle",
    fill: (atm: number) => [
      { action: "SELL", instrument_type: "CE", strike: atm + 100, order_type: "MARKET", qty: 1 },
      { action: "SELL", instrument_type: "PE", strike: atm - 100, order_type: "MARKET", qty: 1 },
    ],
  },
];

function OrderRow({ order, onCancel }: { order: any; onCancel: () => void }) {
  return (
    <tr className="border-b border-bg-border/50 text-xs font-mono hover:bg-bg-hover/30">
      <td className="py-2 text-accent-blue">{order.symbol?.split(":")[1] || order.symbol}</td>
      <td className={clsx("py-2", order.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
        {order.action}
      </td>
      <td className="py-2 text-text-muted">{order.order_type}</td>
      <td className="py-2">{order.qty}</td>
      <td className="py-2">{order.price || "--"}</td>
      <td className="py-2">
        <span className={clsx(
          "px-2 py-0.5 rounded text-xs",
          order.status === "FILLED" ? "bg-accent-green/20 text-accent-green" :
          order.status === "OPEN" ? "bg-accent-blue/20 text-accent-blue" :
          "bg-text-muted/20 text-text-muted"
        )}>
          {order.status}
        </span>
      </td>
      <td className="py-2">
        {["OPEN", "PENDING"].includes(order.status) && (
          <button onClick={onCancel} className="text-accent-red hover:text-red-400">
            <X size={12} />
          </button>
        )}
      </td>
    </tr>
  );
}


export default function TradingPage() {
  const { mode } = useStore();
  const qc = useQueryClient();

  const [symbol, setSymbol] = useState("NSE:NIFTY50-INDEX");
  const [expiry, setExpiry] = useState("");
  const [strike, setStrike] = useState<number | "">("");
  const [optionType, setOptionType] = useState<InstrumentType>("CE");
  const [action, setAction] = useState<ActionType>("BUY");
  const [orderType, setOrderType] = useState<OrderType>("MARKET");
  const [qty, setQty] = useState(1);
  const [price, setPrice] = useState<number | "">("");
  const [sl, setSl] = useState<number | "">("");
  const [target, setTarget] = useState<number | "">("");
  const [isBracket, setIsBracket] = useState(false);
  const [ltp, setLtp] = useState<number | "">("");

  const { data: orders } = useQuery({
    queryKey: ["orders"],
    queryFn: () => getOrders().then((r) => r.data),
    refetchInterval: 3000,
  });

  const { data: positions } = useQuery({
    queryKey: ["positions"],
    queryFn: () => getPositions().then((r) => r.data as PortfolioPosition[]),
    refetchInterval: 5000,
  });

  const { data: trades } = useQuery({
    queryKey: ["trades"],
    queryFn: () => getTrades().then((r) => r.data as PortfolioTrade[]),
    refetchInterval: 10000,
  });

  const { data: agentStatus } = useQuery({
    queryKey: ["strategyAgentStatus"],
    queryFn: () => getStrategyAgentStatus().then((r) => r.data as StrategyAgentStatus),
    refetchInterval: 15000,
    staleTime: 10000,
  });

  const placeMut = useMutation({
    mutationFn: placeOrder,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
    },
  });

  const cancelMut = useMutation({
    mutationFn: cancelOrder,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["trades"] });
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const payload: any = {
      symbol,
      exchange: "NSE",
      action,
      order_type: orderType,
      qty,
      instrument_type: optionType,
      price: price || undefined,
      sl: sl || undefined,
      target: target || undefined,
      expiry: expiry || undefined,
      strike: strike || undefined,
      option_type: optionType,
      ltp: ltp || undefined,
    };
    placeMut.mutate(payload);
  };

  const positionList = positions || [];
  const tradeList = trades || [];
  const manualOpenPositions = positionList.filter((position) => position.qty > 0);

  return (
    <div className="max-w-screen-xl space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold font-mono text-text-primary">Trading Desk</h1>
          <div className="mt-1 text-xs text-text-muted">
            Manual execution sits alongside the autonomous paper agent. Strategy scanning runs automatically during market hours.
          </div>
        </div>
        <div className="rounded border border-accent-blue/20 bg-accent-blue/10 px-3 py-2 text-xs text-accent-blue">
          Agent loop active. No manual strategy trigger is required.
        </div>
      </div>

      <StrategyStatusPanel agentStatus={agentStatus} />
      <StrategyMonitorSection agentStatus={agentStatus} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Order Form */}
        <div className="lg:col-span-1 card p-4">
          <h2 className="text-sm text-text-secondary mb-4 flex items-center gap-2">
            <Plus size={14} /> New Order
            <span className={clsx(
              "ml-auto text-xs px-2 py-0.5 rounded font-bold",
              mode === "paper" ? "bg-accent-green/20 text-accent-green" : "bg-accent-amber/20 text-accent-amber"
            )}>
              {mode.toUpperCase()}
            </span>
          </h2>

          <form onSubmit={handleSubmit} className="space-y-3">
            {/* Symbol */}
            <div>
              <label className="text-xs text-text-muted block mb-1">Symbol</label>
              <input
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="terminal-input w-full text-sm"
                placeholder="NSE:NIFTY50-INDEX"
              />
            </div>

            {/* Expiry + Strike */}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-text-muted block mb-1">Expiry</label>
                <input
                  type="date"
                  value={expiry}
                  onChange={(e) => setExpiry(e.target.value)}
                  className="terminal-input w-full text-sm"
                />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">Strike</label>
                <input
                  type="number"
                  value={strike}
                  onChange={(e) => setStrike(e.target.value ? Number(e.target.value) : "")}
                  className="terminal-input w-full text-sm"
                  placeholder="22500"
                />
              </div>
            </div>

            {/* Instrument Type */}
            <div className="grid grid-cols-4 gap-1">
              {(["CE", "PE", "FUT", "EQ"] as InstrumentType[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setOptionType(t)}
                  className={clsx(
                    "py-1.5 rounded text-xs font-bold border transition-colors",
                    optionType === t
                      ? t === "CE" ? "border-accent-green bg-accent-green/20 text-accent-green"
                        : t === "PE" ? "border-accent-red bg-accent-red/20 text-accent-red"
                        : "border-accent-blue bg-accent-blue/20 text-accent-blue"
                      : "border-bg-border text-text-muted hover:border-bg-hover"
                  )}
                >
                  {t}
                </button>
              ))}
            </div>

            {/* Action */}
            <div className="grid grid-cols-2 gap-2">
              {(["BUY", "SELL"] as ActionType[]).map((a) => (
                <button
                  key={a}
                  type="button"
                  onClick={() => setAction(a)}
                  className={clsx(
                    "py-2 rounded text-sm font-bold border transition-all",
                    action === a && a === "BUY" ? "bg-accent-green/20 border-accent-green text-accent-green" :
                    action === a && a === "SELL" ? "bg-accent-red/20 border-accent-red text-accent-red" :
                    "border-bg-border text-text-muted hover:border-bg-hover"
                  )}
                >
                  {a}
                </button>
              ))}
            </div>

            {/* Order Type */}
            <div className="grid grid-cols-4 gap-1">
              {(["MARKET", "LIMIT", "SL", "SL_M"] as OrderType[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  onClick={() => setOrderType(t)}
                  className={clsx(
                    "py-1 rounded text-xs border transition-colors",
                    orderType === t ? "border-accent-blue bg-accent-blue/20 text-accent-blue" : "border-bg-border text-text-muted"
                  )}
                >
                  {t}
                </button>
              ))}
            </div>

            {/* Qty + LTP */}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-text-muted block mb-1">Qty (Lots)</label>
                <input type="number" value={qty} onChange={(e) => setQty(Number(e.target.value))} min={1} className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">LTP (Paper)</label>
                <input type="number" value={ltp} onChange={(e) => setLtp(e.target.value ? Number(e.target.value) : "")} className="terminal-input w-full text-sm" placeholder="for MKT" />
              </div>
            </div>

            {/* Price / SL / Target */}
            {orderType !== "MARKET" && (
              <div>
                <label className="text-xs text-text-muted block mb-1">Limit Price</label>
                <input type="number" value={price} onChange={(e) => setPrice(e.target.value ? Number(e.target.value) : "")} className="terminal-input w-full text-sm" />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-text-muted block mb-1">Stop Loss</label>
                <input type="number" value={sl} onChange={(e) => setSl(e.target.value ? Number(e.target.value) : "")} className="terminal-input w-full text-sm" />
              </div>
              <div>
                <label className="text-xs text-text-muted block mb-1">Target</label>
                <input type="number" value={target} onChange={(e) => setTarget(e.target.value ? Number(e.target.value) : "")} className="terminal-input w-full text-sm" />
              </div>
            </div>

            {/* Bracket toggle */}
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" checked={isBracket} onChange={(e) => setIsBracket(e.target.checked)} className="rounded" />
              <span className="text-xs text-text-muted">Bracket Order (SL + Target)</span>
            </label>

            <button
              type="submit"
              disabled={placeMut.isPending}
              className={clsx(
                "w-full py-2.5 rounded font-bold text-sm transition-all",
                action === "BUY"
                  ? "bg-accent-green/20 border border-accent-green text-accent-green hover:bg-accent-green/30"
                  : "bg-accent-red/20 border border-accent-red text-accent-red hover:bg-accent-red/30",
                placeMut.isPending && "opacity-50 cursor-not-allowed"
              )}
            >
              {placeMut.isPending ? "Placing..." : `Place ${action} Order`}
            </button>

            {placeMut.isSuccess && (
              <p className="text-accent-green text-xs text-center">Order placed successfully</p>
            )}
            {placeMut.isError && (
              <p className="text-accent-red text-xs text-center">
                {(placeMut.error as any)?.response?.data?.detail || "Order failed"}
              </p>
            )}
          </form>
        </div>

        {/* Quick Strategies + Orders */}
        <div className="lg:col-span-2 space-y-4">
          {/* Quick Strategies */}
          <div className="card p-4">
            <h2 className="text-sm text-text-secondary mb-3 flex items-center gap-2">
              <Zap size={14} /> Quick Strategies
            </h2>
            <div className="flex gap-2 flex-wrap">
              {STRATEGIES.map((s) => (
                <button
                  key={s.name}
                  className="px-3 py-1.5 rounded text-xs bg-bg-secondary border border-bg-border text-text-secondary hover:border-accent-blue hover:text-accent-blue transition-colors"
                >
                  {s.name}
                </button>
              ))}
            </div>
          </div>

          {/* Orders Table */}
          <div className="card p-4">
            <h2 className="text-sm text-text-secondary mb-3">Recent Orders</h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-text-muted border-b border-bg-border">
                    <th className="text-left pb-2">Symbol</th>
                    <th className="text-left pb-2">Side</th>
                    <th className="text-left pb-2">Type</th>
                    <th className="text-right pb-2">Qty</th>
                    <th className="text-right pb-2">Price</th>
                    <th className="text-left pb-2">Status</th>
                    <th className="text-right pb-2">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {orders && orders.length > 0 ? orders.map((o: any, i: number) => (
                    <OrderRow key={i} order={o} onCancel={() => cancelMut.mutate(o.order_id)} />
                  )) : (
                    <tr><td colSpan={7} className="py-6 text-center text-text-muted">No orders</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div className="card p-4">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm text-text-secondary">Portfolio Positions</h2>
            <div className="text-xs text-text-muted">{manualOpenPositions.length} open</div>
          </div>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 text-left">Contract</th>
                  <th className="pb-2 text-left">Side</th>
                  <th className="pb-2 text-right">Qty</th>
                  <th className="pb-2 text-right">Avg</th>
                  <th className="pb-2 text-right">LTP</th>
                  <th className="pb-2 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {manualOpenPositions.length ? (
                  manualOpenPositions.map((position, index) => (
                    <tr key={`${position.symbol}-${index}`} className="border-b border-bg-border/40">
                      <td className="py-2 text-text-primary">{position.symbol?.split(":")[1] || position.symbol}</td>
                      <td className={clsx("py-2", position.action === "BUY" ? "text-accent-green" : "text-accent-red")}>
                        {position.action}
                      </td>
                      <td className="py-2 text-right text-text-primary">{position.qty}</td>
                      <td className="py-2 text-right text-text-primary">{position.avg_price?.toFixed(2) || "--"}</td>
                      <td className="py-2 text-right text-text-primary">{position.ltp?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-2 text-right font-semibold", (position.unrealized_pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                        {formatSigned(position.unrealized_pnl, 2)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="py-6 text-center text-text-muted">
                      No manual portfolio positions.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card p-4">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-sm text-text-secondary">Trade History</h2>
            <div className="text-xs text-text-muted">{tradeList.length} closed trades</div>
          </div>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="border-b border-bg-border text-text-muted">
                  <th className="pb-2 text-left">Contract</th>
                  <th className="pb-2 text-left">Side</th>
                  <th className="pb-2 text-right">Qty</th>
                  <th className="pb-2 text-right">Entry</th>
                  <th className="pb-2 text-right">Exit</th>
                  <th className="pb-2 text-right">P&L</th>
                </tr>
              </thead>
              <tbody>
                {tradeList.length ? (
                  tradeList.slice().reverse().map((trade, index) => (
                    <tr key={`${trade.symbol}-${trade.exit_time}-${index}`} className="border-b border-bg-border/40">
                      <td className="py-2 text-text-primary">{trade.symbol?.split(":")[1] || trade.symbol}</td>
                      <td className={clsx("py-2", trade.action === "BUY" ? "text-accent-green" : "text-accent-red")}>{trade.action}</td>
                      <td className="py-2 text-right text-text-primary">{trade.qty}</td>
                      <td className="py-2 text-right text-text-primary">{trade.entry_price?.toFixed(2) || "--"}</td>
                      <td className="py-2 text-right text-text-primary">{trade.exit_price?.toFixed(2) || "--"}</td>
                      <td className={clsx("py-2 text-right font-semibold", (trade.pnl || 0) >= 0 ? "text-accent-green" : "text-accent-red")}>
                        {formatSigned(trade.pnl, 2)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={6} className="py-6 text-center text-text-muted">
                      No closed trades yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

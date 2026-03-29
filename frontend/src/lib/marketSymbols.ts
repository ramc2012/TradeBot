export const MARKET_INDEX_SYMBOLS = [
  "NSE:NIFTY50-INDEX",
  "NSE:BANKNIFTY-INDEX",
  "NSE:FINNIFTY-INDEX",
  "NSE:MIDCPNIFTY-INDEX",
] as const;

export type MarketIndexSymbol = (typeof MARKET_INDEX_SYMBOLS)[number];

export const MARKET_INDEX_LABELS: Record<MarketIndexSymbol, string> = {
  "NSE:NIFTY50-INDEX": "NIFTY",
  "NSE:BANKNIFTY-INDEX": "BANKNIFTY",
  "NSE:FINNIFTY-INDEX": "FINNIFTY",
  "NSE:MIDCPNIFTY-INDEX": "MIDCPNIFTY",
};

export function getMarketIndexLabel(symbol: string): string {
  return MARKET_INDEX_LABELS[symbol as MarketIndexSymbol] ?? symbol;
}


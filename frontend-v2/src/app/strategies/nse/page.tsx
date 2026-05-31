import DeskStub from "@/components/strategies/DeskStub";

export default function Page() {
  return (
    <DeskStub
      title="NSE Index strategy"
      description="30-min ATM MACD on NIFTY/BANKNIFTY/SENSEX + the 5-min index+MP variant."
      v1Href="http://localhost:3000/strategy"
      v1Label="Open NSE strategy in v1"
    />
  );
}

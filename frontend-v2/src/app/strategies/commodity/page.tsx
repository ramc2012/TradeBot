import DeskStub from "@/components/strategies/DeskStub";

export default function Page() {
  return (
    <DeskStub
      title="Commodity desk"
      description="MCX commodity desk: 8 instrument rows with MP profile, CVD/VWAP, trigger badge, modal drilldown."
      v1Href="http://localhost:3000/commodity"
      v1Label="Open Commodity desk in v1"
    />
  );
}

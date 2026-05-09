import SectorDetailPage from "@/components/sector-interaction/SectorDetailPage";

export default function Page({ params }: { params: { sector: string } }) {
  return <SectorDetailPage sectorKey={params.sector} />;
}

import SectorDetailPage from "@/components/v1-sector-interaction/SectorDetailPage";

export default function Page({ params }: { params: { sector: string } }) {
  return <SectorDetailPage sectorKey={params.sector} />;
}

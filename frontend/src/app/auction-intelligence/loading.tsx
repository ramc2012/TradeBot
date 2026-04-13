export default function AuctionIntelligenceLoading() {
  return (
    <div className="mx-auto max-w-[1600px] space-y-6 pb-10">
      <section className="rounded-[30px] border border-white/10 bg-[linear-gradient(180deg,rgba(11,18,30,0.95),rgba(7,11,21,0.98))] px-6 py-6 md:px-8">
        <div className="h-4 w-40 animate-pulse rounded-full bg-white/10" />
        <div className="mt-4 h-10 max-w-3xl animate-pulse rounded-2xl bg-white/10" />
        <div className="mt-4 h-5 max-w-2xl animate-pulse rounded-full bg-white/10" />
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <div className="h-12 animate-pulse rounded-2xl bg-white/10" />
          <div className="h-12 animate-pulse rounded-2xl bg-white/10" />
          <div className="h-12 animate-pulse rounded-2xl bg-white/10" />
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-6">
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className="h-36 animate-pulse rounded-[26px] border border-white/8 bg-white/5" />
        ))}
      </section>

      <section className="grid gap-6 xl:grid-cols-2">
        <div className="h-[520px] animate-pulse rounded-[30px] border border-white/10 bg-white/5" />
        <div className="h-[520px] animate-pulse rounded-[30px] border border-white/10 bg-white/5" />
      </section>
    </div>
  );
}

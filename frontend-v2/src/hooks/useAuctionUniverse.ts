"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
export function useAuctionUniverse() {
  return useQuery<{ symbols: string[]; stock_count: number }>({
    queryKey: ["auction", "universe"],
    queryFn: async () => (await api.get("/api/auction-intelligence/universe")).data,
    staleTime: 300_000,
    refetchInterval: 300_000,
  });
}

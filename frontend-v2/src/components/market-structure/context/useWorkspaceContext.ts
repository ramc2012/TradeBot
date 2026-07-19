"use client";

/**
 * useWorkspaceContext — URL ⇄ context, and the only writer of either.
 *
 * Mirrors the pattern already proven by `useUrlTab` (desk-ui/DeskShell): read
 * from `useSearchParams`, write with `router.replace(..., {scroll:false})` so
 * the workspace never scroll-jumps and the back button walks the context
 * history naturally.
 *
 * ATOMICITY: `setCtx` takes a PARTIAL and performs exactly one URL mutation.
 * Changing market+symbol+contract together is one navigation, so every panel
 * re-derives in the same render pass — no panel can be showing NIFTY while its
 * neighbour shows BANKNIFTY.
 */
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import {
  DEFAULT_SYMBOL,
  type WorkspaceContext,
  parseContext,
  serializeContext,
} from "./schema";

export type WorkspaceContextApi = {
  ctx: WorkspaceContext;
  /** One atomic context mutation → one URL replace. */
  setCtx: (patch: Partial<WorkspaceContext>) => void;
  /**
   * True when the trader has typed a past time frontier. It does NOT mean the
   * data moved — no wired endpoint accepts an as-of (see `context/schema.ts`) —
   * so this exists only so the UI can say, loudly, that the field is not
   * applied. It must never be used to derive a replay/historical data mode.
   */
  asOfPinnedButUnapplied: boolean;
};

export function useWorkspaceContext(): WorkspaceContextApi {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const search = searchParams?.toString() ?? "";
  const ctx = useMemo(() => parseContext(new URLSearchParams(search)), [search]);

  const setCtx = useCallback(
    (patch: Partial<WorkspaceContext>) => {
      const current = parseContext(new URLSearchParams(search));
      const next: WorkspaceContext = { ...current, ...patch };

      // Switching market re-pins to that market's default instrument unless the
      // caller named one explicitly — an MCX workspace pinned to NIFTY would be
      // a context that cannot resolve.
      if (patch.market && patch.market !== current.market && !patch.symbol) {
        next.symbol = DEFAULT_SYMBOL[patch.market];
        next.contract = null;
      }
      if (patch.symbol && patch.symbol !== current.symbol && patch.contract === undefined) {
        next.contract = null;
      }
      // NOTE: `asOf` deliberately does NOT set any replay/suppression flag.
      // Deriving one here was the mechanism by which a July-15 URL painted
      // REPLAY over the July-17 snapshot.
      next.symbol = String(next.symbol || "").toUpperCase();

      const qs = serializeContext(next).toString();
      router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
    },
    [router, pathname, search],
  );

  return { ctx, setCtx, asOfPinnedButUnapplied: ctx.asOf !== "now" };
}

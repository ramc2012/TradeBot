/**
 * The pane fan-out, as a PURE object with no React in it.
 *
 * It was extracted out of `LinkedChartProvider` for one reason: a shared
 * crosshair and a shared zoom are the two features on this canvas that look
 * like they work while being wrong, and a source-scan test cannot tell the
 * difference. With the fan-out sitting here it can be driven directly by fake
 * charts in `tests/market-canvas.test.ts` — the echo guard, the "peer has no
 * value ⇒ clear rather than invent" rule and the late-mount range adoption are
 * all asserted behaviourally.
 *
 * Contract (unchanged from the provider that used to own it):
 *   ZOOM / PAN   a pane publishes its visible LOGICAL range; peers get it
 *                applied. The broadcast sets a flag first, so a peer's echo is
 *                ignored instead of ping-ponging forever.
 *   CROSSHAIR    the pane under the pointer publishes the hovered TIME; each
 *                peer places its crosshair at ITS OWN value for that time, and
 *                CLEARS if it has none. No interpolation, no zero fallback.
 *   SELECTION    a click publishes the bar time to the owner.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

export type PaneRegistration = {
  chart: any;
  series: any;
  /**
   * This pane's own value at a given (already tz-shifted) chart time.
   * `null` means "nothing here", and the pane's crosshair is cleared rather
   * than placed at a fabricated price.
   */
  priceAt: (time: number) => number | null;
};

type Entry = PaneRegistration & {
  onRange: (range: any) => void;
  onCrosshair: (param: any) => void;
  onClick: (param: any) => void;
};

export type PaneRegistry = {
  register: (id: string, reg: PaneRegistration) => void;
  unregister: (id: string) => void;
  size: () => number;
};

export function createPaneRegistry(opts: {
  onSelect: (time: number | null) => void;
  onSizeChange?: (size: number) => void;
}): PaneRegistry {
  const panes = new Map<string, Entry>();
  let applyingRange = false;
  let applyingCrosshair = false;

  const detach = (entry: Entry, id: string) => {
    try {
      entry.chart.timeScale().unsubscribeVisibleLogicalRangeChange(entry.onRange);
      entry.chart.unsubscribeCrosshairMove(entry.onCrosshair);
      entry.chart.unsubscribeClick(entry.onClick);
    } catch {
      /* the chart may already be removed — nothing to detach from */
    }
    panes.delete(id);
  };

  const register = (id: string, reg: PaneRegistration) => {
    // Re-registration (a pane remounted) must not leave stale subscriptions
    // pointing at a removed chart.
    const previous = panes.get(id);
    if (previous) detach(previous, id);

    const onRange = (range: any) => {
      if (!range || applyingRange) return;
      applyingRange = true;
      try {
        panes.forEach((peer, key) => {
          if (key === id) return;
          try {
            peer.chart.timeScale().setVisibleLogicalRange(range);
          } catch {
            /* a pane disposed mid-broadcast is not an error */
          }
        });
      } finally {
        applyingRange = false;
      }
    };

    const onCrosshair = (param: any) => {
      if (applyingCrosshair) return;
      applyingCrosshair = true;
      try {
        const time = param?.time as number | undefined;
        panes.forEach((peer, key) => {
          if (key === id) return;
          try {
            if (time == null) {
              peer.chart.clearCrosshairPosition();
              return;
            }
            const value = peer.priceAt(Number(time));
            // No observation on this pane at that time ⇒ NO crosshair.
            if (value == null || !Number.isFinite(value)) {
              peer.chart.clearCrosshairPosition();
              return;
            }
            peer.chart.setCrosshairPosition(value, time, peer.series);
          } catch {
            /* ignore panes that have gone away */
          }
        });
      } finally {
        applyingCrosshair = false;
      }
    };

    const onClick = (param: any) => {
      const time = param?.time;
      opts.onSelect(time == null ? null : Number(time));
    };

    const entry: Entry = { ...reg, onRange, onCrosshair, onClick };
    panes.set(id, entry);
    try {
      reg.chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
      reg.chart.subscribeCrosshairMove(onCrosshair);
      reg.chart.subscribeClick(onClick);
    } catch {
      /* a chart that refuses subscription simply stays unlinked */
    }
    opts.onSizeChange?.(panes.size);

    // Adopt whichever peer already has a range, so a pane that mounts second
    // lands on the trader's current viewport instead of its own.
    let peer: Entry | undefined;
    panes.forEach((candidate, key) => {
      if (!peer && key !== id) peer = candidate;
    });
    if (peer) {
      try {
        const range = (peer as Entry).chart.timeScale().getVisibleLogicalRange();
        if (range) {
          applyingRange = true;
          reg.chart.timeScale().setVisibleLogicalRange(range);
          applyingRange = false;
        }
      } catch {
        applyingRange = false;
      }
    }
  };

  const unregister = (id: string) => {
    const entry = panes.get(id);
    if (!entry) return;
    detach(entry, id);
    opts.onSizeChange?.(panes.size);
  };

  return { register, unregister, size: () => panes.size };
}

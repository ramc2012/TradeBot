"use client";

/**
 * LinkedChartProvider — ONE timeline shared by every pane of the Structure view.
 *
 * lightweight-charts gives each chart its own private time scale and crosshair.
 * Two stacked charts therefore drift the moment the trader pans one of them,
 * and a crosshair in the price pane says nothing about the flow pane. This
 * provider is the React seam that makes them one canvas; the fan-out itself
 * lives in `pane-registry.ts` as a plain object, so the behaviour that is easy
 * to get subtly wrong (echo ping-pong, inventing a peer value, a late-mounting
 * pane resetting the viewport) is driven by fake charts in the test suite
 * instead of being asserted by grepping this file.
 *
 *   ZOOM / PAN   every registered pane publishes its visible LOGICAL range;
 *                the registry re-applies it to the peers.
 *   CROSSHAIR    the pane under the pointer publishes the hovered TIME; peers
 *                place their own crosshair at THEIR OWN series value for that
 *                time, and CLEAR when they have none — an invented point on a
 *                flow pane is exactly the class of lie this terminal exists to
 *                remove.
 *   SELECTION    a click pins a bar time, which drives the bar inspector.
 *
 * VIEWPORT PRESERVATION. Nothing here calls `fitContent`. Panes own their own
 * fit, gated on `fitKey` (the mechanism already shipped in `CandleChart`), so a
 * periodic data refresh cannot yank the viewport — and because every pane in a
 * group shares one fitKey, a genuine instrument or timeframe change re-fits
 * them together.
 */
import { createContext, useContext, useMemo, useRef, useState } from "react";

import { createPaneRegistry, type PaneRegistration } from "./pane-registry";

export type { PaneRegistration };

type LinkedChartApi = {
  register: (id: string, reg: PaneRegistration) => void;
  unregister: (id: string) => void;
  /** Chart time of the clicked bar, or null. Shifted by the pane tz offset. */
  selectedTime: number | null;
  setSelectedTime: (time: number | null) => void;
  /** How many panes are currently linked — surfaced so the UI can say so. */
  paneCount: number;
};

const NOOP: LinkedChartApi = {
  register: () => {},
  unregister: () => {},
  selectedTime: null,
  setSelectedTime: () => {},
  paneCount: 0,
};

const LinkedChartContext = createContext<LinkedChartApi>(NOOP);

export function useLinkedChart(): LinkedChartApi {
  return useContext(LinkedChartContext);
}

export function LinkedChartProvider({ children }: { children: React.ReactNode }) {
  const [selectedTime, setSelectedTime] = useState<number | null>(null);
  const [paneCount, setPaneCount] = useState(0);

  const registryRef = useRef<ReturnType<typeof createPaneRegistry> | null>(null);
  if (!registryRef.current) {
    registryRef.current = createPaneRegistry({
      onSelect: setSelectedTime,
      onSizeChange: setPaneCount,
    });
  }
  const registry = registryRef.current;

  const value = useMemo<LinkedChartApi>(
    () => ({
      register: registry.register,
      unregister: registry.unregister,
      selectedTime,
      setSelectedTime,
      paneCount,
    }),
    [registry, selectedTime, paneCount],
  );

  return <LinkedChartContext.Provider value={value}>{children}</LinkedChartContext.Provider>;
}

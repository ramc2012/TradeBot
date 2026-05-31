/**
 * Server-side layout for /strategies/*.
 *
 * Intentionally minimal — the per-desk shell (header / tabs / right
 * rail) lives in the DeskShell client component so each page can
 * supply its own tab definitions and rightSlot content.
 */
export default function StrategiesLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

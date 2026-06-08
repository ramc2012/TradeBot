"use client";

/**
 * AuthTokenGate — in-app unlock for the API write token.
 *
 * When the backend has APP_TOKEN_AUTH_ENABLED on, every /api/* call needs the
 * `x-nomad-write-token` header. The api client reads it from
 * localStorage["nomad_write_token"]. This modal lets the operator paste that
 * token once from the UI (no browser devtools), and pops up automatically when:
 *   - no token is set on load, or
 *   - any /api/* request returns 403 (the api interceptor emits `nomad:auth-required`).
 *
 * The token is stored only in this browser. On save we reload so every query
 * refires with the header (this is also what unblocks the Fyers login flow).
 */
import { useCallback, useEffect, useState } from "react";

const TOKEN_KEY = "nomad_write_token";

function hasToken(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return Boolean(window.localStorage.getItem(TOKEN_KEY)?.trim());
  } catch {
    return true;
  }
}

export default function AuthTokenGate() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    // Open ONLY when the backend actually demands auth (a 403 from /api/*). When
    // auth is disabled (paper-trading test phase) no 403 fires, so the modal stays
    // dormant — no token prompt, no friction. Open on first 403 once per load.
    const onRequired = () => {
      if (!hasToken()) setOpen(true);
    };
    const onManual = () => setOpen(true);
    window.addEventListener("nomad:auth-required", onRequired);
    window.addEventListener("nomad:open-token-gate", onManual);
    return () => {
      window.removeEventListener("nomad:auth-required", onRequired);
      window.removeEventListener("nomad:open-token-gate", onManual);
    };
  }, []);

  const save = useCallback(() => {
    const v = value.trim();
    if (!v) return;
    try {
      window.localStorage.setItem(TOKEN_KEY, v);
    } catch {
      /* ignore */
    }
    setSaved(true);
    setTimeout(() => window.location.reload(), 400);
  }, [value]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl border border-bg-border bg-bg-card p-5 shadow-2xl">
        <div className="text-[15px] font-semibold text-text-primary">Unlock workspace</div>
        <p className="mt-2 text-[12.5px] leading-relaxed text-text-secondary">
          This workspace requires the API write token. Paste your{" "}
          <code className="rounded bg-bg-primary/50 px-1 font-mono text-[11.5px]">APP_WRITE_TOKEN</code>{" "}
          (from the server <code className="font-mono text-[11.5px]">.env</code>) once — it is stored only in
          this browser and sent with each request. This also unblocks the broker login flow.
        </p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
          }}
          placeholder="Paste APP_WRITE_TOKEN"
          className="mt-3 w-full rounded-lg border border-bg-border bg-bg-primary/40 px-3 py-2 font-mono text-[13px] text-text-primary outline-none focus:border-bg-active"
        />
        <div className="mt-3 flex items-center justify-between gap-2">
          <button
            onClick={() => setOpen(false)}
            className="rounded-lg px-3 py-1.5 text-[12px] text-text-muted hover:text-text-secondary"
          >
            Later
          </button>
          <button
            onClick={save}
            disabled={!value.trim() || saved}
            className="rounded-lg bg-accent-green/20 px-4 py-1.5 text-[12.5px] font-medium text-accent-green hover:bg-accent-green/30 disabled:opacity-50"
          >
            {saved ? "Saved — reloading…" : "Unlock"}
          </button>
        </div>
      </div>
    </div>
  );
}

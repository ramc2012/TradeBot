"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { CheckCircle2, KeyRound, Loader2, ShieldAlert } from "lucide-react";

const TOKEN_KEY = "nomad_write_token";
const APP_TOKEN_GATE_ENABLED = process.env.NEXT_PUBLIC_APP_TOKEN_AUTH_ENABLED === "true";

function readCookie(name: string): string {
  if (typeof document === "undefined") {
    return "";
  }
  const prefix = `${name}=`;
  const match = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));
  return match ? decodeURIComponent(match.slice(prefix.length)) : "";
}

function readSavedToken(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return (
    window.localStorage.getItem(TOKEN_KEY)?.trim()
    || readCookie(TOKEN_KEY).trim()
    || ""
  );
}

function writeSavedToken(token: string) {
  window.localStorage.setItem(TOKEN_KEY, token);
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${TOKEN_KEY}=${encodeURIComponent(token)}; path=/; SameSite=Lax${secure}`;
}

function isCloudRunHost(): boolean {
  if (!APP_TOKEN_GATE_ENABLED) {
    return false;
  }
  if (typeof window === "undefined") {
    return false;
  }
  const host = window.location.hostname.toLowerCase();
  return host.endsWith(".run.app") || host.endsWith(".a.run.app");
}

export default function AppTokenGate({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [token, setToken] = useState("");
  const [input, setInput] = useState("");
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState("");
  const requiresToken = useMemo(isCloudRunHost, []);

  useEffect(() => {
    if (!requiresToken) {
      setReady(true);
      return;
    }
    const saved = readSavedToken();
    if (saved) {
      setToken(saved);
      setInput(saved);
    }
    setReady(true);
  }, [requiresToken]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = input.trim();
    if (!next) {
      setError("Enter the cloud access token.");
      return;
    }
    setValidating(true);
    setError("");
    try {
      const response = await fetch("/api/trading/strategy-agent/status", {
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "x-nomad-write-token": next,
        },
      });
      if (!response.ok) {
        setError(response.status === 403 ? "Token was rejected." : `Validation failed with ${response.status}.`);
        return;
      }
      writeSavedToken(next);
      setToken(next);
      window.location.reload();
    } catch {
      setError("Could not validate the token. Check the connection and try again.");
    } finally {
      setValidating(false);
    }
  }

  if (!ready) {
    return null;
  }
  if (!requiresToken || token) {
    return <>{children}</>;
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg-primary px-4 text-text-primary">
      <div className="w-full max-w-md rounded-xl border border-bg-border bg-bg-secondary/45 p-5 shadow-2xl">
        <div className="flex items-start gap-3">
          <div className="rounded-lg border border-accent-blue/25 bg-accent-blue/10 p-2 text-accent-blue">
            <KeyRound size={18} />
          </div>
          <div>
            <h1 className="font-mono text-lg font-semibold">Cloud Access</h1>
            <p className="mt-1 text-sm text-text-muted">
              Enter the app write token to open this trading workspace.
            </p>
          </div>
        </div>
        <form onSubmit={handleSubmit} className="mt-5 space-y-3">
          <input
            autoFocus
            value={input}
            onChange={(event) => setInput(event.target.value)}
            type="password"
            placeholder="APP_WRITE_TOKEN"
            className="terminal-input w-full"
            autoComplete="off"
          />
          {error ? (
            <div className="flex items-center gap-2 rounded-lg border border-accent-red/20 bg-accent-red/8 px-3 py-2 text-xs text-accent-red">
              <ShieldAlert size={14} />
              {error}
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded-lg border border-accent-green/20 bg-accent-green/8 px-3 py-2 text-xs text-accent-green">
              <CheckCircle2 size={14} />
              The token is saved only in this browser.
            </div>
          )}
          <button
            type="submit"
            disabled={validating}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-accent-blue/30 bg-accent-blue/15 px-3 py-2 text-sm font-semibold text-accent-blue transition-colors hover:bg-accent-blue/25 disabled:opacity-50"
          >
            {validating ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
            Unlock Workspace
          </button>
        </form>
      </div>
    </div>
  );
}

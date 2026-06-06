const DEFAULT_BACKEND_PORT = (process.env.NEXT_PUBLIC_BACKEND_PORT || "8000").trim();

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function deriveCloudRunBackendHost(hostname: string): string | null {
  const normalized = String(hostname || "").trim();
  if (!normalized || !normalized.includes("-ui-")) {
    return null;
  }
  if (!normalized.endsWith(".run.app") && !normalized.endsWith(".a.run.app")) {
    return null;
  }
  return normalized.replace("-ui-", "-api-");
}

function isLocalHostname(hostname: string): boolean {
  const normalized = String(hostname || "").trim().toLowerCase();
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "0.0.0.0";
}

export function resolveApiBaseUrlCandidates(): string[] {
  const configured = (process.env.NEXT_PUBLIC_API_URL || "").trim();
  if (configured) {
    return [trimTrailingSlash(configured)];
  }

  // Unconfigured: use a SAME-ORIGIN RELATIVE base ("") identically on the server
  // and the client. This is critical for two reasons:
  //   1. Determinism — the old code returned "http://localhost:8000" during SSR
  //      but window.location.origin on the client, so the API badge text (and any
  //      API_URL-derived markup) differed between server and client. That tripped
  //      React hydration errors (#418/#423/#425) which blanked the desk until React
  //      re-rendered, so every panel flashed "no data" before the queries ran.
  //   2. Correctness — a relative base routes requests through the same-origin Next
  //      proxy at app/api/[...path]/route.ts (which forwards to the backend via the
  //      server-side API_URL env), so there's no CORS and no host guessing.
  // The cloud-run / localhost host derivation is dropped (the GCP UI is retired and
  // local dev sets NEXT_PUBLIC_API_URL); the proxy covers every deployed topology.
  return [""];
}

export function resolveApiBaseUrl(): string {
  return resolveApiBaseUrlCandidates()[0] ?? "";
}

export function resolveWebSocketBaseUrl(): string {
  const configured = (process.env.NEXT_PUBLIC_WS_URL || "").trim();
  if (configured) {
    return trimTrailingSlash(configured);
  }

  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const hostname = window.location.hostname || "localhost";
    const cloudRunBackendHost = deriveCloudRunBackendHost(hostname);
    if (cloudRunBackendHost) {
      return trimTrailingSlash(`${protocol}//${cloudRunBackendHost}`);
    }
    if (window.location.port && window.location.port !== DEFAULT_BACKEND_PORT) {
      return trimTrailingSlash(`${protocol}//${hostname}:${DEFAULT_BACKEND_PORT}`);
    }
    const host = isLocalHostname(hostname)
      ? `${hostname}:${DEFAULT_BACKEND_PORT}`
      : window.location.host;
    return trimTrailingSlash(`${protocol}//${host}`);
  }

  return "ws://localhost:8000";
}

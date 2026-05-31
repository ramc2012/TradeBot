const DEFAULT_BACKEND_PORT = (process.env.NEXT_PUBLIC_BACKEND_PORT || "8000").trim();

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function pushUnique(values: string[], candidate: string | null | undefined): void {
  const normalized = trimTrailingSlash(String(candidate || "").trim());
  if (!normalized || values.includes(normalized)) {
    return;
  }
  values.push(normalized);
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
  const candidates: string[] = [];
  if (configured) {
    pushUnique(candidates, configured);
    return candidates;
  }

  if (typeof window === "undefined") {
    return ["http://localhost:8000"];
  }

  const protocol = window.location.protocol === "https:" ? "https:" : "http:";
  const origin = `${protocol}//${window.location.host}`;
  const hostname = window.location.hostname || "localhost";
  const cloudRunBackendHost = deriveCloudRunBackendHost(hostname);
  if (cloudRunBackendHost) {
    pushUnique(candidates, origin);
  }
  if (cloudRunBackendHost) {
    pushUnique(candidates, `${protocol}//${cloudRunBackendHost}`);
  }
  if (isLocalHostname(hostname)) {
    pushUnique(candidates, `${protocol}//${hostname}:${DEFAULT_BACKEND_PORT}`);
  }
  pushUnique(candidates, origin);
  return candidates;
}

export function resolveApiBaseUrl(): string {
  const candidates = resolveApiBaseUrlCandidates();
  return candidates[0] || "http://localhost:8000";
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

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

export function resolveApiBaseUrl(): string {
  const configured = (process.env.NEXT_PUBLIC_API_URL || "").trim();
  if (configured) {
    return trimTrailingSlash(configured);
  }

  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    const hostname = window.location.hostname || "localhost";
    const cloudRunBackendHost = deriveCloudRunBackendHost(hostname);
    if (cloudRunBackendHost) {
      return trimTrailingSlash(`${protocol}//${cloudRunBackendHost}`);
    }
    return trimTrailingSlash(`${protocol}//${hostname}:${DEFAULT_BACKEND_PORT}`);
  }

  return "http://localhost:8000";
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
    return trimTrailingSlash(`${protocol}//${hostname}:${DEFAULT_BACKEND_PORT}`);
  }

  return "ws://localhost:8000";
}

"use client";

import { resolveApiBaseUrl, resolveWebSocketBaseUrl } from "./runtime-url";

interface ReconnectingWS {
  close: () => void;
}

type WebSocketTokenResponse = {
  token: string;
  expires_at?: string;
};

let cachedWebSocketToken: string | null = null;
let cachedWebSocketTokenExpiry = 0;
let webSocketTokenPromise: Promise<string> | null = null;

async function getWebSocketToken(): Promise<string> {
  const now = Date.now();
  if (cachedWebSocketToken && now < cachedWebSocketTokenExpiry - 30_000) {
    return cachedWebSocketToken;
  }
  if (webSocketTokenPromise) {
    return webSocketTokenPromise;
  }

  webSocketTokenPromise = fetch(`${resolveApiBaseUrl()}/api/auth/ws-token`, {
    method: "GET",
    headers: { Accept: "application/json" },
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`ws token fetch failed with ${response.status}`);
      }
      const payload = (await response.json()) as WebSocketTokenResponse;
      cachedWebSocketToken = payload.token;
      cachedWebSocketTokenExpiry = payload.expires_at
        ? new Date(payload.expires_at).getTime()
        : Date.now() + 5 * 60 * 1000;
      return payload.token;
    })
    .finally(() => {
      webSocketTokenPromise = null;
    });

  return webSocketTokenPromise;
}

function withWebSocketToken(url: string, token: string): string {
  const parsed = new URL(url);
  parsed.searchParams.set("auth", token);
  return parsed.toString();
}

function withQuery(url: string, query: Record<string, string | null | undefined>): string {
  const parsed = new URL(url);
  for (const [key, value] of Object.entries(query)) {
    if (value == null || value === "") continue;
    parsed.searchParams.set(key, value);
  }
  return parsed.toString();
}

/**
 * Creates a WebSocket with automatic exponential-backoff reconnection.
 * Returns a handle with a single close() method to permanently disconnect.
 */
function createReconnectingSocket(
  url: string,
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  let ws: WebSocket | null = null;
  let stopped = false;
  let retryCount = 0;
  let retryTimeout: ReturnType<typeof setTimeout> | null = null;

  function scheduleReconnect() {
    if (stopped) return;
    onStatusChange?.(false);
    const delay = Math.min(1000 * Math.pow(2, retryCount), 30_000);
    retryCount++;
    retryTimeout = setTimeout(() => {
      void connect();
    }, delay);
  }

  async function connect() {
    if (stopped) return;
    try {
      const token = await getWebSocketToken();
      if (stopped) return;
      ws = new WebSocket(withWebSocketToken(url, token));
    } catch {
      scheduleReconnect();
      return;
    }

    ws.onopen = () => {
      retryCount = 0;
      onStatusChange?.(true);
    };

    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data));
      } catch {
        // ignore malformed
      }
    };

    ws.onerror = () => {
      // onclose fires after onerror — handle retry there
    };

    ws.onclose = () => {
      if (stopped) return;
      scheduleReconnect();
    };
  }

  void connect();

  return {
    close() {
      stopped = true;
      if (retryTimeout) clearTimeout(retryTimeout);
      ws?.close();
    },
  };
}

export function createTickSocket(
  symbol: string,
  onMessage: (data: unknown) => void,
): ReconnectingWS {
  return createReconnectingSocket(
    `${resolveWebSocketBaseUrl()}/ws/ticks/${encodeURIComponent(symbol)}`,
    onMessage,
  );
}

export function createPositionsSocket(
  onMessage: (data: unknown) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/positions`, onMessage);
}

export function createProposalsSocket(
  onMessage: (data: unknown) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/proposals`, onMessage);
}

export function createLayoutSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/layout`, onMessage, onStatusChange);
}

export function createSystemOverviewSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/system-overview`, onMessage, onStatusChange);
}

export function createSystemHealthSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/system-health`, onMessage, onStatusChange);
}

export function createStrategyOverviewSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/strategy-overview`, onMessage, onStatusChange);
}

export function createStrategyDashboardSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/strategy-dashboard`, onMessage, onStatusChange);
}

export function createPositionsOverviewSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/positions-overview`, onMessage, onStatusChange);
}

export function createCommodityOverviewSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/commodity-overview`, onMessage, onStatusChange);
}

export function createCommodityWatchlistSocket(
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/commodity-watchlist`, onMessage, onStatusChange);
}

export function createMarketWatchlistSocket(
  expiry: string,
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(
    withQuery(`${resolveWebSocketBaseUrl()}/ws/market-watchlist`, { expiry }),
    onMessage,
    onStatusChange,
  );
}

export function createFractalMarketProfileSocket(
  symbol: string,
  onMessage: (data: unknown) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${resolveWebSocketBaseUrl()}/ws/fractal-market-profile/${encodeURIComponent(symbol)}`, onMessage, onStatusChange);
}

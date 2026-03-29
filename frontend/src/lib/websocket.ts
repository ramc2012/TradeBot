"use client";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";

interface ReconnectingWS {
  close: () => void;
}

/**
 * Creates a WebSocket with automatic exponential-backoff reconnection.
 * Returns a handle with a single close() method to permanently disconnect.
 */
function createReconnectingSocket(
  url: string,
  onMessage: (data: object) => void,
  onStatusChange?: (connected: boolean) => void,
): ReconnectingWS {
  let ws: WebSocket | null = null;
  let stopped = false;
  let retryCount = 0;
  let retryTimeout: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (stopped) return;
    ws = new WebSocket(url);

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
      onStatusChange?.(false);
      // Exponential backoff: 1s, 2s, 4s, 8s … capped at 30s
      const delay = Math.min(1000 * Math.pow(2, retryCount), 30_000);
      retryCount++;
      retryTimeout = setTimeout(connect, delay);
    };
  }

  connect();

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
  onMessage: (data: object) => void,
): ReconnectingWS {
  return createReconnectingSocket(
    `${WS_URL}/ws/ticks/${encodeURIComponent(symbol)}`,
    onMessage,
  );
}

export function createPositionsSocket(
  onMessage: (data: object) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${WS_URL}/ws/positions`, onMessage);
}

export function createProposalsSocket(
  onMessage: (data: object) => void,
): ReconnectingWS {
  return createReconnectingSocket(`${WS_URL}/ws/proposals`, onMessage);
}

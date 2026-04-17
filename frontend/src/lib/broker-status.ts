export type BrokerStatusEntry = {
  broker: string;
  connected: boolean;
  ready?: boolean;
  session_active?: boolean;
  state?: string | null;
  detail?: string | null;
  source?: string | null;
  checked_at?: string | null;
  needs_reconnect?: boolean;
  user_id?: string | null;
  name?: string | null;
  connected_at?: string | null;
};

export function isBrokerReady(status?: Partial<BrokerStatusEntry> | null): boolean {
  return Boolean(status?.ready ?? status?.connected);
}

export function hasBrokerSession(status?: Partial<BrokerStatusEntry> | null): boolean {
  return Boolean(status?.session_active ?? status?.connected ?? status?.ready);
}

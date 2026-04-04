"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getBrokerStatus, getCredentialsStatus, saveCredentials, getFyersAuthUrl, getUpstoxAuthUrl, connectUpstox,
  getIciciLoginUrl, connectIciciBreeze, connectFivepaisa,
  disconnectBroker, getRiskStatus, updateRiskConfig, getResearchCacheStatus,
  getTelegramSettings, saveTelegramSettings, discoverTelegramChats, sendTelegramTest,
} from "@/lib/api";
import { api } from "@/lib/api";
import { clsx } from "clsx";
import {
  CheckCircle2, XCircle, Eye, EyeOff, ExternalLink, RefreshCw,
  ChevronDown, ChevronUp, Loader2, Plug, Unplug, AlertCircle, Save,
  Copy, Info, Send,
} from "lucide-react";

// ── Helpers ───────────────────────────────────────────────────────────────────

function PasswordInput({ value, onChange, placeholder, label, saved }: {
  value: string; onChange: (v: string) => void; placeholder?: string; label?: string; saved?: boolean;
}) {
  const [show, setShow] = useState(false);
  return (
    <div>
      {label && <label className="text-xs text-text-muted block mb-1">{label}</label>}
      <div className="relative">
        <input type={show ? "text" : "password"} value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={saved && !value ? "●●●●● saved" : placeholder}
          className={clsx(
            "terminal-input w-full text-sm pr-8",
            saved && !value && "border-l-2 border-l-accent-green placeholder:text-accent-green"
          )} />
        <button type="button" onClick={() => setShow(!show)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary">
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

function TextInput({ value, onChange, placeholder, label, readOnly, saved }: {
  value: string; onChange?: (v: string) => void; placeholder?: string; label?: string; readOnly?: boolean; saved?: boolean;
}) {
  return (
    <div>
      {label && <label className="text-xs text-text-muted block mb-1">{label}</label>}
      <input value={value} onChange={(e) => onChange?.(e.target.value)}
        placeholder={saved && !value ? "●●●●● saved" : placeholder}
        readOnly={readOnly}
        className={clsx(
          "terminal-input w-full text-sm",
          readOnly && "opacity-70 cursor-default",
          saved && !value && "border-l-2 border-l-accent-green placeholder:text-accent-green"
        )} />
    </div>
  );
}

function CopyableUrl({ url, label }: { url: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <label className="text-xs text-text-muted block mb-1">{label}</label>
      <div className="flex items-center gap-2">
        <code className="flex-1 text-xs bg-bg-secondary border border-bg-border rounded px-2 py-1.5 text-accent-green font-mono truncate">
          {url}
        </code>
        <button onClick={() => { navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
          className="shrink-0 text-text-muted hover:text-text-primary p-1" title="Copy">
          {copied ? <CheckCircle2 size={13} className="text-accent-green" /> : <Copy size={13} />}
        </button>
      </div>
    </div>
  );
}

function StatusBadge({ connected }: { connected: boolean }) {
  return (
    <span className={clsx(
      "flex items-center gap-1 text-xs font-bold px-2 py-0.5 rounded",
      connected ? "bg-accent-green/15 text-accent-green" : "bg-text-muted/15 text-text-muted"
    )}>
      {connected ? <CheckCircle2 size={10} /> : <XCircle size={10} />}
      {connected ? "CONNECTED" : "DISCONNECTED"}
    </span>
  );
}

function formatInteger(value?: number | null): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(value);
}

function formatDecimal(value?: number | null, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  return new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return "—";
  const total = Math.round(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return `${total}s`;
}

function CredsSavedBadge({ fields }: { fields: Record<string, boolean> }) {
  const total = Object.keys(fields).length;
  const filled = Object.values(fields).filter(Boolean).length;
  if (!filled) return null;
  const all = filled === total;
  return (
    <span className={clsx(
      "flex items-center gap-1 text-xs px-2 py-0.5 rounded",
      all ? "bg-accent-blue/15 text-accent-blue" : "bg-accent-amber/15 text-accent-amber"
    )}>
      <Save size={9} />
      {all ? "Creds saved" : `${filled}/${total} saved`}
    </span>
  );
}

function useAllCredsStatus() {
  return useQuery({
    queryKey: ["allCredsStatus"],
    queryFn: () => api.get("/api/auth/all-credentials-status").then(r => r.data),
    staleTime: 30_000,
  });
}

function UpstoxApiBudgetCard() {
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["settingsResearchCacheStatus"],
    queryFn: () => getResearchCacheStatus().then((r) => r.data),
    refetchInterval: 30000,
    staleTime: 15000,
  });

  const budget = data?.api_budget;
  const scheduler = data?.scheduler;

  return (
    <div className="card p-4 space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-semibold text-sm">Upstox API Budget</div>
          <p className="text-xs text-text-muted mt-1">
            Rolling call usage, configured pacing, and theoretical useful-dataset ETA for the research sync.
          </p>
        </div>
        <button onClick={() => refetch()} className="text-text-muted hover:text-text-primary p-1 rounded" title="Refresh">
          <RefreshCw size={14} />
        </button>
      </div>

      {isLoading && (
        <div className="text-xs text-text-muted flex items-center gap-2">
          <Loader2 size={12} className="animate-spin" /> Loading API budget…
        </div>
      )}

      {isError && (
        <div className="text-xs text-accent-red flex items-center gap-2">
          <AlertCircle size={12} /> Could not load API budget telemetry.
        </div>
      )}

      {!isLoading && !isError && budget && (
        <>
          <div className="grid gap-3 md:grid-cols-4">
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Rolling 30m</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatInteger(budget.rolling_30m?.calls)} / {formatInteger(budget.limits?.per_30_minutes)}
              </div>
              <div className="text-xs text-text-muted">
                {formatDecimal(budget.rolling_30m?.utilization_pct_of_doc_limit, 1)}% of documented cap
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Configured Ceiling</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatInteger(budget.configured?.calls_per_30_minutes)} / {formatInteger(budget.limits?.per_30_minutes)}
              </div>
              <div className="text-xs text-text-muted">
                gap {formatDecimal(budget.configured?.gap_seconds, 1)}s between calls
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Last Run Rate</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatDecimal(budget.last_run?.avg_calls_per_second, 2)}/s
              </div>
              <div className="text-xs text-text-muted">
                {formatInteger(budget.last_run?.calls)} calls in {formatDuration(budget.last_run?.elapsed_seconds)}
              </div>
            </div>
            <div className="rounded border border-bg-border bg-bg-secondary/40 p-3">
              <div className="text-[11px] uppercase tracking-wide text-text-muted">Full Useful Target</div>
              <div className="mt-1 text-lg font-semibold text-text-primary">
                {formatDuration(budget.theoretical?.full_seconds_at_configured_rate)}
              </div>
              <div className="text-xs text-text-muted">
                configured-rate estimate from empty cache
              </div>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-bg-border bg-bg-secondary/30 p-3 space-y-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Current Rates vs Limits</div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Observed avg / min</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.rolling_30m?.avg_calls_per_minute, 1)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Doc max / min</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.limits?.per_minute)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Observed avg / sec</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.rolling_30m?.avg_calls_per_second, 3)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Configured / sec</div>
                  <div className="font-semibold text-text-primary">{formatDecimal(budget.configured?.calls_per_second, 3)}</div>
                </div>
              </div>
              <div className="text-xs text-text-muted">
                Scheduler: <span className="text-text-primary">{scheduler?.label || "—"}</span>
              </div>
            </div>

            <div className="rounded border border-bg-border bg-bg-secondary/30 p-3 space-y-2">
              <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Useful-Dataset ETA</div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Total estimated calls</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.theoretical?.total_calls)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Remaining estimated calls</div>
                  <div className="font-semibold text-text-primary">{formatInteger(budget.theoretical?.remaining_calls)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Full ETA at observed rate</div>
                  <div className="font-semibold text-text-primary">{formatDuration(budget.theoretical?.full_seconds_at_observed_rate)}</div>
                </div>
                <div className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">Remaining ETA at observed rate</div>
                  <div className="font-semibold text-text-primary">{formatDuration(budget.theoretical?.remaining_seconds_at_observed_rate)}</div>
                </div>
              </div>
              <div className="text-[11px] text-text-muted">
                Model: 1 expiry fetch per underlying, 1 spot-history fetch per underlying, 1 contract-discovery fetch per expiry, and 1 historical-candle fetch per required CE/PE research contract.
              </div>
            </div>
          </div>

          <div className="rounded border border-bg-border bg-bg-secondary/30 p-3 space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Call Mix In Last 30 Minutes</div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4 text-xs">
              {Object.entries(budget.rolling_30m?.by_endpoint || {}).map(([endpoint, count]) => (
                <div key={endpoint} className="rounded bg-bg-primary/40 px-2 py-2 border border-bg-border">
                  <div className="text-text-muted">{endpoint.replaceAll("_", " ")}</div>
                  <div className="font-semibold text-text-primary">{formatInteger(Number(count))}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function TelegramCard() {
  const qc = useQueryClient();
  const { data: savedStatus } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = {
    bot_token: Boolean(savedStatus?.telegram?.fields?.bot_token),
    chat_id: Boolean(savedStatus?.telegram?.fields?.chat_id),
  };
  const { data, isLoading } = useQuery({
    queryKey: ["telegramSettings"],
    queryFn: () => getTelegramSettings().then((r) => r.data),
    staleTime: 15000,
  });

  const [expanded, setExpanded] = useState(false);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [interval, setInterval] = useState("1h");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!data) return;
    setEnabled(Boolean(data.enabled));
    setInterval(String(data.report_interval || "1h"));
  }, [data]);

  const saveMut = useMutation({
    mutationFn: saveTelegramSettings,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["telegramSettings"] });
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
      setMsg("✓ Telegram settings saved");
      setBotToken("");
      setChatId("");
    },
    onError: (error: any) => {
      setMsg(error?.response?.data?.detail || "Failed to save Telegram settings");
    },
  });

  const discoverMut = useMutation({
    mutationFn: () => discoverTelegramChats(botToken.trim()),
    onError: (error: any) => {
      setMsg(error?.response?.data?.detail || "Failed to discover Telegram chats");
    },
  });

  const testMut = useMutation({
    mutationFn: () => sendTelegramTest(),
    onSuccess: (res) => {
      setMsg(res.data?.message || "Telegram test message sent.");
    },
    onError: (error: any) => {
      setMsg(error?.response?.data?.detail || "Failed to send Telegram test message");
    },
  });

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="w-8 h-8 rounded bg-accent-blue/15 flex items-center justify-center shrink-0">
            <span className="text-accent-blue font-bold text-xs">TG</span>
          </div>
          <div>
            <div className="font-semibold text-sm">Telegram Trade Reports</div>
            <div className="text-xs text-text-muted">
              Send paper-strategy statistics at the selected interval.
            </div>
          </div>
          <CredsSavedBadge fields={savedFields} />
          {data?.enabled && data?.has_destination && (
            <span className="flex items-center gap-1 text-xs font-bold px-2 py-0.5 rounded bg-accent-green/15 text-accent-green">
              <CheckCircle2 size={10} />
              ACTIVE
            </span>
          )}
        </div>
        <button onClick={() => setExpanded(!expanded)} className="text-text-muted hover:text-text-primary p-1">
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </button>
      </div>

      {expanded && (
        <div className="space-y-3 pt-2 border-t border-bg-border">
          <div className="grid gap-3 md:grid-cols-2">
            <PasswordInput
              value={botToken}
              onChange={setBotToken}
              label="Bot Token"
              placeholder="123456:ABC..."
              saved={savedFields["bot_token"]}
            />
            <TextInput
              value={chatId}
              onChange={setChatId}
              label="Chat ID"
              placeholder="-1001234567890"
              saved={savedFields["chat_id"]}
            />
          </div>

          <div className="rounded border border-bg-border bg-bg-secondary/20 p-3 space-y-2">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-semibold uppercase tracking-wide text-text-secondary">Auto Detect Chat ID</div>
                <div className="text-xs text-text-muted">
                  Uses the bot token to read recent Telegram updates and list chats the bot has already seen.
                </div>
              </div>
              <button
                onClick={() => {
                  setMsg("");
                  discoverMut.mutate();
                }}
                disabled={discoverMut.isPending || (!botToken.trim() && !savedFields["bot_token"])}
                className="px-3 py-1.5 rounded text-xs bg-bg-hover border border-bg-border text-text-secondary hover:border-accent-blue/40 disabled:opacity-50 flex items-center gap-1"
              >
                {discoverMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />}
                Detect Chats
              </button>
            </div>
            <div className="text-[11px] text-text-muted">
              If nothing is found, send <code className="px-1 rounded bg-bg-primary/40 text-accent-green">/start</code> to the bot in a private chat or add the bot to the target group/channel and create one update there, then detect again.
            </div>
            {discoverMut.data?.data?.chats?.length ? (
              <div className="space-y-2">
                {discoverMut.data.data.chats.map((chat: any) => (
                  <div key={chat.chat_id} className="flex items-center justify-between gap-3 rounded border border-bg-border bg-bg-primary/30 px-3 py-2 text-xs">
                    <div>
                      <div className="text-text-primary font-semibold">{chat.title}</div>
                      <div className="text-text-muted">
                        {chat.type} {chat.username ? `| @${chat.username}` : ""} | {chat.chat_id}
                      </div>
                    </div>
                    <button
                      onClick={() => setChatId(chat.chat_id)}
                      className="px-2 py-1 rounded border border-accent-blue/30 bg-accent-blue/10 text-accent-blue hover:bg-accent-blue/20"
                    >
                      Use
                    </button>
                  </div>
                ))}
              </div>
            ) : discoverMut.isSuccess ? (
              <div className="text-xs text-text-muted">{discoverMut.data?.data?.hint || "No chats were discovered yet."}</div>
            ) : null}
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="text-xs text-text-muted block mb-1">Report Interval</label>
              <select
                value={interval}
                onChange={(e) => setInterval(e.target.value)}
                className="terminal-input w-full text-sm"
              >
                <option value="30m">Every 30 minutes</option>
                <option value="1h">Hourly</option>
                <option value="4h">Every 4 hours</option>
                <option value="daily">Daily</option>
              </select>
            </div>
            <label className="flex items-center gap-2 rounded border border-bg-border bg-bg-secondary/30 px-3 py-2 text-sm text-text-secondary">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="rounded"
              />
              Enable Telegram reports
            </label>
          </div>

          <div className="text-xs text-text-muted">
            {isLoading
              ? "Loading current Telegram settings…"
              : data?.has_destination
                ? `Current interval: ${data.report_interval}. Reports are ${data.enabled ? "enabled" : "disabled"}.`
                : "Save a bot token and chat ID to enable Telegram delivery."}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => {
                setMsg("");
                saveMut.mutate({
                  bot_token: botToken,
                  chat_id: chatId,
                  enabled,
                  report_interval: interval,
                });
              }}
              disabled={saveMut.isPending}
              className="px-3 py-1.5 rounded text-xs bg-accent-blue/15 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/25 disabled:opacity-50 flex items-center gap-1"
            >
              {saveMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}
              Save Telegram Settings
            </button>
            <button
              onClick={() => {
                setMsg("");
                testMut.mutate();
              }}
              disabled={testMut.isPending || !data?.has_destination}
              className="px-3 py-1.5 rounded text-xs bg-accent-green/15 border border-accent-green/30 text-accent-green hover:bg-accent-green/25 disabled:opacity-50 flex items-center gap-1"
            >
              {testMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
              Send Test Message
            </button>
          </div>
          {msg && <p className={clsx("text-xs", msg.startsWith("✓") ? "text-accent-green" : "text-accent-red")}>{msg}</p>}
        </div>
      )}
    </div>
  );
}

// ── Fyers Card ────────────────────────────────────────────────────────────────

const FYERS_DEFAULT_CALLBACK = "http://localhost:8000/api/auth/fyers/callback";

function FyersCard({ status, onRefresh }: { status: any; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.fyers?.fields ?? {};
  const hasCreds = allCreds?.fyers?.has_credentials;
  const { data: fyersCreds } = useQuery({
    queryKey: ["credentialsStatus", "fyers"],
    queryFn: () => getCredentialsStatus("fyers").then((r) => r.data),
    staleTime: 15000,
  });

  const [expanded, setExpanded] = useState(false);
  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const effectiveRedirectUri = String(fyersCreds?.display?.redirect_uri || redirectUri || FYERS_DEFAULT_CALLBACK);

  useEffect(() => {
    if (fyersCreds?.display?.app_id && !appId) {
      setAppId(String(fyersCreds.display.app_id));
    }
    if (fyersCreds?.display?.redirect_uri && !redirectUri) {
      setRedirectUri(String(fyersCreds.display.redirect_uri));
    }
  }, [fyersCreds, appId, redirectUri]);

  const handleSaveCreds = async () => {
    const redirectToSave = redirectUri.trim();
    const savedRedirect = String(fyersCreds?.display?.redirect_uri || "").trim();
    if (!appId || !secret) { setMsg("Enter APP ID and Secret"); return; }
    if (!redirectToSave && !savedRedirect) {
      setMsg("Enter the Fyers redirect URI you actually registered in the app settings");
      return;
    }
    setSaving(true); setMsg("");
    try {
      await saveCredentials("fyers", {
        app_id: appId,
        secret,
        ...(redirectToSave ? { redirect_uri: redirectToSave } : {}),
      });
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
      qc.invalidateQueries({ queryKey: ["credentialsStatus", "fyers"] });
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Failed to save"); }
    finally { setSaving(false); }
  };

  const handleGetUrl = async () => {
    try {
      const r = await getFyersAuthUrl();
      window.open(r.data.auth_url, "_blank", "width=600,height=700");
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Save credentials first"); }
  };

  const handleConnect = async () => {
    if (!authCode) { setMsg("Paste the auth_code from the callback URL"); return; }
    setSaving(true); setMsg("");
    try {
      const { connectBroker } = await import("@/lib/api");
      await connectBroker("fyers", { auth_code: authCode });
      setMsg("✓ Connected!"); setExpanded(false); onRefresh();
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Connection failed"); }
    finally { setSaving(false); }
  };

  useEffect(() => {
    const h = (e: MessageEvent) => {
      if (e.data?.broker === "fyers" && e.data?.status === "connected") {
        setExpanded(false); onRefresh();
      }
    };
    window.addEventListener("message", h);
    return () => window.removeEventListener("message", h);
  }, [onRefresh]);

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="w-8 h-8 rounded bg-accent-amber/15 flex items-center justify-center shrink-0">
            <span className="text-accent-amber font-bold text-xs">FY</span>
          </div>
          <div>
            <div className="font-semibold text-sm">Fyers</div>
            {status?.connected && <div className="text-xs text-text-muted">{status.name}</div>}
          </div>
          <StatusBadge connected={status?.connected} />
          {!status?.connected && <CredsSavedBadge fields={savedFields} />}
        </div>
        <div className="flex items-center gap-1">
          {status?.connected && (
            <button onClick={() => disconnectBroker("fyers").then(onRefresh)}
              className="text-text-muted hover:text-accent-red p-1 rounded" title="Disconnect">
              <Unplug size={14} />
            </button>
          )}
          <button onClick={() => setExpanded(!expanded)} className="text-text-muted hover:text-text-primary p-1">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="space-y-4 pt-2 border-t border-bg-border">
          {/* Step 0: Redirect URL */}
          <div className="bg-accent-amber/5 border border-accent-amber/20 rounded p-3 space-y-2">
            <div className="text-xs font-bold text-accent-amber flex items-center gap-1">
              <Info size={11} /> Register this Redirect URL in your Fyers app first
            </div>
            <CopyableUrl url={effectiveRedirectUri} label="Saved Redirect URL (register this in myapi.fyers.in → App Settings)" />
            <p className="text-xs text-text-muted">
              Go to{" "}
              <a href="https://myapi.fyers.in" target="_blank" rel="noopener noreferrer"
                className="text-accent-blue hover:underline inline-flex items-center gap-1">
                myapi.fyers.in <ExternalLink size={9} />
              </a>
              {" "}→ My Apps → Edit app → add the redirect URL above → Save.
            </p>
          </div>

          {hasCreds && (
            <div className="bg-accent-blue/5 border border-accent-blue/20 rounded p-2 text-xs text-accent-blue flex items-center gap-2">
              <CheckCircle2 size={12} /> Credentials saved — go to Step 2 to login.
            </div>
          )}

          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 1 — API Credentials</div>
            <TextInput value={appId} onChange={setAppId} label="APP ID (Client ID)" placeholder="XXXXXXXX-100" saved={savedFields["app_id"]} />
            <PasswordInput value={secret} onChange={setSecret} label="Secret Key" placeholder="secret" saved={savedFields["secret"]} />
            <TextInput value={redirectUri} onChange={setRedirectUri}
              label="Redirect URI (must match app settings)" placeholder={FYERS_DEFAULT_CALLBACK} saved={savedFields["redirect_uri"]} />
            <button onClick={handleSaveCreds} disabled={saving}
              className="px-3 py-1.5 rounded text-xs bg-bg-hover border border-bg-border text-text-secondary hover:border-accent-amber/40 disabled:opacity-50 flex items-center gap-1">
              {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Credentials
            </button>
          </div>

          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 2 — Authorize</div>
            <button onClick={handleGetUrl}
              className="w-full py-2 rounded text-sm bg-accent-amber/15 border border-accent-amber/30 text-accent-amber hover:bg-accent-amber/25 flex items-center justify-center gap-2">
              <ExternalLink size={14} /> Open Fyers Login Page
            </button>
            <p className="text-xs text-text-muted">
              After login you'll be redirected to{" "}
              <code className="text-accent-green bg-bg-secondary px-1 rounded">…?auth_code=XXXX</code>.
              Copy the <code className="text-accent-green bg-bg-secondary px-1 rounded">auth_code</code> value.
            </p>
            <PasswordInput value={authCode} onChange={setAuthCode} label="Auth Code from redirect URL" placeholder="paste auth_code here" />
            <button onClick={handleConnect} disabled={saving || !authCode}
              className="w-full py-2 rounded text-sm bg-accent-blue/20 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/30 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect Fyers
            </button>
          </div>
          {msg && <p className={clsx("text-xs", msg.startsWith("✓") ? "text-accent-green" : "text-accent-red")}>{msg}</p>}
        </div>
      )}
    </div>
  );
}

// ── Upstox Card ───────────────────────────────────────────────────────────────

const UPSTOX_CALLBACK = "http://localhost:8000/api/auth/upstox/callback";

function UpstoxCard({ status, onRefresh }: { status: any; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.upstox?.fields ?? {};
  const hasCreds = allCreds?.upstox?.has_credentials;

  const [expanded, setExpanded] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [secret, setSecret] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  // Sandbox app redirect is always https://www.google.com
  const UPSTOX_REDIRECT = "https://www.google.com";

  const handleSaveCreds = async () => {
    if (!apiKey || !secret) { setMsg("Enter both API Key and Secret"); return; }
    setSaving(true); setMsg("");
    try {
      await saveCredentials("upstox", { api_key: apiKey, secret, redirect_uri: UPSTOX_REDIRECT });
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Failed"); }
    finally { setSaving(false); }
  };

  const handleOpenLogin = async () => {
    setSaving(true); setMsg("");
    try {
      const r = await getUpstoxAuthUrl();
      window.open(r.data.auth_url, "_blank");
      setMsg("Login page opened. After login, Google will open — copy the code= value from the URL and paste in Step 3.");
    } catch (e: any) {
      setMsg(e?.response?.data?.detail || "Save credentials first");
    } finally { setSaving(false); }
  };

  const handleConnect = async () => {
    const code = authCode.trim();
    if (!code) { setMsg("Paste the authorization code or access token"); return; }
    setSaving(true); setMsg("Connecting…");
    try {
      await connectUpstox(code);
      setMsg("✓ Connected!"); setExpanded(false); onRefresh();
    } catch (e: any) {
      // Surface as much detail as possible for debugging
      const resp = e?.response;
      const rawDetail = resp?.data?.detail;
      const detail: string =
        typeof rawDetail === "string" ? rawDetail :
        Array.isArray(rawDetail) ? JSON.stringify(rawDetail) :
        resp?.data ? JSON.stringify(resp.data).slice(0, 200) :
        e?.message || "Connection failed — check backend logs";
      const d = detail.toLowerCase();
      if (d.includes("udapi100068") || d.includes("redirect_uri")) {
        setMsg("Redirect URI mismatch — set your Upstox app Redirect URL to https://www.google.com");
      } else if (d.includes("expired") || (d.includes("invalid") && !d.includes("token exchange"))) {
        setMsg("⏰ Code expired — codes are single-use. Open Login again and paste the fresh code immediately.");
      } else {
        setMsg(`❌ ${detail}`);
      }
    } finally { setSaving(false); }
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="w-8 h-8 rounded bg-accent-blue/15 flex items-center justify-center shrink-0">
            <span className="text-accent-blue font-bold text-xs">UP</span>
          </div>
          <div>
            <div className="font-semibold text-sm">Upstox</div>
            {status?.connected && <div className="text-xs text-text-muted">{status.name}</div>}
          </div>
          <StatusBadge connected={status?.connected} />
          {!status?.connected && <CredsSavedBadge fields={savedFields} />}
          <span className="text-xs bg-accent-blue/10 text-accent-blue px-2 py-0.5 rounded border border-accent-blue/20">
            1yr F&O history
          </span>
          <span className="text-xs bg-bg-hover text-text-muted px-2 py-0.5 rounded border border-bg-border">
            Sandbox
          </span>
        </div>
        <div className="flex items-center gap-1">
          {status?.connected && (
            <button onClick={() => disconnectBroker("upstox").then(onRefresh)}
              className="text-text-muted hover:text-accent-red p-1 rounded"><Unplug size={14} /></button>
          )}
          <button onClick={() => setExpanded(!expanded)} className="text-text-muted hover:text-text-primary p-1">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="space-y-4 pt-2 border-t border-bg-border">
          {/* Sandbox info */}
          <div className="bg-accent-blue/5 border border-accent-blue/20 rounded p-3 space-y-1 text-xs">
            <div className="font-bold text-accent-blue flex items-center gap-1"><Info size={11} /> Sandbox App — Manual Code Flow</div>
            <p className="text-text-muted">
              Sandbox apps redirect to <code className="text-accent-green bg-bg-secondary px-1 rounded">https://www.google.com</code>.
              After login, copy the <code className="text-accent-green bg-bg-secondary px-1 rounded">code=</code> value from Google's URL bar and paste below.
            </p>
          </div>

          {hasCreds && (
            <div className="bg-accent-green/5 border border-accent-green/20 rounded p-2 text-xs text-accent-green flex items-center gap-2">
              <CheckCircle2 size={12} /> Credentials saved — proceed to Step 2.
            </div>
          )}

          {/* Step 1 — Credentials */}
          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 1 — API Credentials (one-time)</div>
            <p className="text-xs text-text-muted">
              From{" "}
              <a href="https://account.upstox.com/developer/apps" target="_blank" rel="noopener noreferrer"
                className="text-accent-blue hover:underline inline-flex items-center gap-1">
                account.upstox.com → Apps <ExternalLink size={9} />
              </a>
              {" "}→ your Sandbox App → API Key and Secret.
            </p>
            <TextInput value={apiKey} onChange={setApiKey} label="API Key" placeholder="your_api_key" saved={savedFields["api_key"]} />
            <PasswordInput value={secret} onChange={setSecret} label="Secret" placeholder="your_secret" saved={savedFields["secret"]} />
            <button onClick={handleSaveCreds} disabled={saving}
              className="px-3 py-1.5 rounded text-xs bg-bg-hover border border-bg-border text-text-secondary hover:border-accent-blue/40 disabled:opacity-50 flex items-center gap-1">
              {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Credentials
            </button>
          </div>

          {/* Step 2 — Open Login */}
          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 2 — Open Upstox Login</div>
            <div className="text-xs text-text-muted space-y-1">
              <p>1. Click the button — Upstox login opens in a new tab.</p>
              <p>2. Log in with your Upstox credentials.</p>
              <p>3. You'll be redirected to Google. The URL will look like:</p>
              <p className="font-mono text-accent-green bg-bg-secondary rounded px-2 py-1 break-all">
                https://www.google.com/?code=<strong>AUTH_CODE_HERE</strong>&state=...
              </p>
              <p>4. Copy everything after <code className="text-accent-green">code=</code> and before <code className="text-accent-green">&</code></p>
            </div>
            <button onClick={handleOpenLogin} disabled={saving}
              className="w-full py-2 rounded text-sm bg-accent-blue/20 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/30 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <ExternalLink size={14} />} Open Upstox Login
            </button>
          </div>

          {/* Step 3 — Paste Code or Token */}
          <div className="space-y-2 bg-bg-secondary/50 rounded p-3 border border-bg-border">
            <div className="text-xs font-bold text-text-secondary">Step 3 — Paste Code or Access Token</div>
            <div className="text-xs text-text-muted space-y-1">
              <p className="text-accent-amber flex items-start gap-1">
                <AlertCircle size={10} className="mt-0.5 shrink-0" /> Auth codes are single-use and expire in ~1 minute. Connect immediately after pasting.
              </p>
              <p>
                <strong className="text-text-secondary">Option A</strong> — Paste the <code className="text-accent-green bg-bg-secondary px-1 rounded">code=</code> value from the Google URL (after Step 2).
              </p>
              <p>
                <strong className="text-text-secondary">Option B</strong> — If you already have a valid Upstox access token (JWT starting with <code className="text-accent-green bg-bg-secondary px-1 rounded">eyJ...</code>), paste it here directly — it will be used as-is without exchange.
              </p>
            </div>
            <TextInput value={authCode} onChange={setAuthCode}
              label="Auth code (from Google URL) or existing access token (eyJ...)"
              placeholder="paste code or eyJ... token here"
              saved={savedFields["access_token"]} />
            {savedFields["access_token"] && !status?.connected && (
              <p className="text-xs text-accent-green/80 flex items-center gap-1">
                <CheckCircle2 size={11} /> Saved access token found — server will auto-connect on next restart. Or paste a fresh token and Connect now.
              </p>
            )}
            <button onClick={handleConnect} disabled={saving || !authCode.trim()}
              className="w-full py-2 rounded text-sm bg-accent-blue/20 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/30 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect Upstox
            </button>
          </div>

          {msg && <p className={clsx("text-xs", msg.startsWith("✓") ? "text-accent-green" : msg.startsWith("Login") ? "text-accent-amber" : "text-accent-red")}>{msg}</p>}
        </div>
      )}
    </div>
  );
}

// ── 5Paisa Card ───────────────────────────────────────────────────────────────

function FivePaisaCard({ status, onRefresh }: { status: any; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.fivepaisa?.fields ?? {};
  const hasCreds = allCreds?.fivepaisa?.has_credentials;
  const allFieldsSaved = hasCreds && Object.values(savedFields).filter(Boolean).length >= 7;

  // Auto-expand when creds are saved so user sees TOTP section immediately
  const [expanded, setExpanded] = useState(false);
  const [fields, setFields] = useState({
    app_name: "", app_source: "", user_id: "", email: "",
    password: "", user_key: "", encryption_key: "",
  });
  const [totp, setTotp] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  // Auto-expand when all credentials are already saved
  useEffect(() => {
    if (allFieldsSaved && !status?.connected) {
      setExpanded(true);
    }
  }, [allFieldsSaved, status?.connected]);

  const setF = (k: string) => (v: string) => setFields(f => ({ ...f, [k]: v }));

  const handleSaveCreds = async () => {
    const filled = Object.values(fields).filter(Boolean).length;
    if (filled === 0) { setMsg("Fill at least one field"); return; }
    setSaving(true); setMsg("");
    try {
      const toSave = Object.fromEntries(Object.entries(fields).filter(([, v]) => v));
      await saveCredentials("fivepaisa", toSave);
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Failed"); }
    finally { setSaving(false); }
  };

  const handleConnect = async () => {
    const trimmedTotp = totp.trim();
    if (trimmedTotp.length !== 6) { setMsg("Enter the 6-digit TOTP from your authenticator app"); return; }
    setSaving(true); setMsg("Connecting…");
    try {
      await connectFivepaisa(trimmedTotp);
      setMsg("✓ Connected!"); setExpanded(false); onRefresh();
    } catch (e: any) {
      const detail = e?.response?.data?.detail || "Connection failed";
      if (detail.toLowerCase().includes("totp") || detail.toLowerCase().includes("otp")) {
        setMsg(`TOTP error — check your authenticator app: ${detail}`);
      } else {
        setMsg(detail);
      }
    } finally { setSaving(false); }
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="w-8 h-8 rounded bg-accent-green/15 flex items-center justify-center shrink-0">
            <span className="text-accent-green font-bold text-xs">5P</span>
          </div>
          <div>
            <div className="font-semibold text-sm">5Paisa</div>
            {status?.connected && <div className="text-xs text-text-muted">{status.name}</div>}
          </div>
          <StatusBadge connected={status?.connected} />
          {!status?.connected && <CredsSavedBadge fields={savedFields} />}
        </div>
        <div className="flex items-center gap-1">
          {status?.connected && (
            <button onClick={() => disconnectBroker("fivepaisa").then(onRefresh)}
              className="text-text-muted hover:text-accent-red p-1 rounded"><Unplug size={14} /></button>
          )}
          <button onClick={() => setExpanded(!expanded)} className="text-text-muted hover:text-text-primary p-1">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="space-y-4 pt-2 border-t border-bg-border">
          <p className="text-xs text-text-muted">
            Credentials from{" "}
            <a href="https://xstream.5paisa.com" target="_blank" rel="noopener noreferrer"
              className="text-accent-blue hover:underline inline-flex items-center gap-1">
              xstream.5paisa.com <ExternalLink size={10} />
            </a>
            {" "}→ Dashboard. <strong className="text-text-secondary">User Key = API Key field</strong> (not Encryption Key).
          </p>

          {allFieldsSaved ? (
            <div className="bg-accent-green/5 border border-accent-green/20 rounded p-2 text-xs text-accent-green flex items-center gap-2">
              <CheckCircle2 size={12} /> All 7 API credentials saved. Enter TOTP below to connect.
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-xs font-bold text-text-secondary">
                Step 1 — API Credentials
                {hasCreds && <span className="ml-2 text-accent-amber font-normal">(partially saved)</span>}
              </div>
              <div className="grid grid-cols-2 gap-2">
                <TextInput value={fields.app_name} onChange={setF("app_name")} label="App Name (App ID)" placeholder="5P56340816" saved={savedFields["app_name"]} />
                <TextInput value={fields.app_source} onChange={setF("app_source")} label="App Source" placeholder="9918" saved={savedFields["app_source"]} />
                <TextInput value={fields.user_id} onChange={setF("user_id")} label="User ID (Client Code)" placeholder="NL0BYabni01" saved={savedFields["user_id"]} />
                <TextInput value={fields.email} onChange={setF("email")} label="Registered Email (for TOTP login)" placeholder="you@example.com" saved={savedFields["email"]} />
                <PasswordInput value={fields.password} onChange={setF("password")} label="User Password" placeholder="password" saved={savedFields["password"]} />
                <PasswordInput value={fields.user_key} onChange={setF("user_key")} label="User Key (= API Key)" placeholder="sQzETAqIr2…" saved={savedFields["user_key"]} />
                <PasswordInput value={fields.encryption_key} onChange={setF("encryption_key")} label="Encryption Key" placeholder="qK5i7sME…" saved={savedFields["encryption_key"]} />
              </div>
              <button onClick={handleSaveCreds} disabled={saving}
                className="px-3 py-1.5 rounded text-xs bg-bg-hover border border-bg-border text-text-secondary hover:border-accent-green/40 disabled:opacity-50 flex items-center gap-1">
                {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Credentials
              </button>
            </div>
          )}

          {/* TOTP section — always visible when expanded */}
          <div className="space-y-2 bg-bg-secondary/50 rounded p-3 border border-bg-border">
            <div className="text-xs font-bold text-text-secondary">
              {allFieldsSaved ? "Connect with TOTP" : "Step 2 — Connect with TOTP"}
            </div>
            <div className="text-xs text-text-muted space-y-1">
              <p>Open your authenticator app (Google Authenticator / Authy) → find <strong className="text-text-primary">5Paisa</strong> → enter the 6-digit code.</p>
              <p className="text-accent-amber flex items-center gap-1">
                <AlertCircle size={10} /> TOTP must be enabled on your 5Paisa account. Codes expire in 30 seconds.
              </p>
            </div>
            <input
              value={totp}
              onChange={(e) => setTotp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="Enter 6-digit TOTP"
              maxLength={6}
              inputMode="numeric"
              className="terminal-input w-full text-sm text-center tracking-[0.3em] font-mono text-lg"
            />
            <button
              onClick={handleConnect}
              disabled={saving || totp.trim().length !== 6}
              className="w-full py-2 rounded text-sm bg-accent-green/20 border border-accent-green/30 text-accent-green hover:bg-accent-green/30 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />}
              {saving ? "Connecting…" : "Connect 5Paisa"}
            </button>
            {totp.length > 0 && totp.length < 6 && (
              <p className="text-xs text-text-muted text-center">{6 - totp.length} more digits needed</p>
            )}
          </div>
          {msg && (
            <p className={clsx("text-xs", msg.startsWith("✓") ? "text-accent-green" : "text-accent-red")}>
              {msg}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// ── ICICI Breeze Card ─────────────────────────────────────────────────────────

function ICICIBreezeCard({ status, onRefresh }: { status: any; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.icici_breeze?.fields ?? {};
  const hasCreds = allCreds?.icici_breeze?.has_credentials;

  const [expanded, setExpanded] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [secret, setSecret] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const [loginUrl, setLoginUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [tokenAge, setTokenAge] = useState(0); // seconds since token was pasted
  const tokenAgeRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const handleSaveCreds = async () => {
    if (!apiKey && !secret) { setMsg("Enter at least one field"); return; }
    setSaving(true); setMsg("");
    try {
      const toSave: Record<string, string> = {};
      if (apiKey) toSave.api_key = apiKey;
      if (secret) toSave.secret = secret;
      await saveCredentials("icici_breeze", toSave);
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Failed"); }
    finally { setSaving(false); }
  };

  const handleGetLoginUrl = async () => {
    setSaving(true); setMsg("");
    try {
      const r = await getIciciLoginUrl();
      setLoginUrl(r.data.login_url);
      window.open(r.data.login_url, "_blank", "width=700,height=700");
      setMsg("Login page opened. After login, copy the apisession= token from your browser URL bar.");
    } catch (e: any) { setMsg(e?.response?.data?.detail || "Save API Key first"); }
    finally { setSaving(false); }
  };

  const handleConnect = async () => {
    if (!sessionToken) { setMsg("Paste the apisession token"); return; }
    setSaving(true); setMsg("");
    try {
      await connectIciciBreeze(sessionToken);
      setMsg("✓ Connected!"); setExpanded(false); onRefresh();
    } catch (e: any) {
      const detail: string = e?.response?.data?.detail || "Connection failed";
      const d = detail.toLowerCase();
      // API Key errors (various sources: SDK wrapper and our new error messages)
      if (
        d.includes("public key") || d.includes("check api key") ||
        d.includes("api key is not recognised") || d.includes("appkey") ||
        d.includes("not recognised by icici") || d.includes("my apps")
      ) {
        setMsg(
          "❌ API Key not registered — your App Key was rejected by ICICI's servers.\n\n" +
          "Fix: Go to https://api.icicidirect.com → 'My Apps' → create/activate your app → " +
          "copy the exact App Key shown there into Step 1 above, then Save Credentials."
        );
      } else if (
        d.includes("session key") || d.includes("invalid session") ||
        d.includes("session token is invalid")
      ) {
        setMsg("⏰ Session token invalid — click 'Open ICICI Direct Login' again and paste the fresh token immediately (tokens expire in ~30 seconds).");
      } else if (d.includes("expired") || d.includes("resource not available")) {
        setMsg("⏰ Token expired — generate a fresh one: click login button → copy apisession → paste & connect immediately.");
      } else {
        // Show the full backend error message as-is (our new messages are user-friendly)
        setMsg(detail);
      }
    } finally { setSaving(false); }
  };

  return (
    <div className="card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <div className="w-8 h-8 rounded bg-accent-red/15 flex items-center justify-center shrink-0">
            <span className="text-accent-red font-bold text-xs">IC</span>
          </div>
          <div>
            <div className="font-semibold text-sm">ICICI Direct <span className="text-xs text-text-muted font-normal">(Breeze)</span></div>
            {status?.connected && <div className="text-xs text-text-muted">{status.name}</div>}
          </div>
          <StatusBadge connected={status?.connected} />
          {!status?.connected && <CredsSavedBadge fields={savedFields} />}
          <span className="text-xs bg-accent-green/10 text-accent-green px-2 py-0.5 rounded border border-accent-green/20">
            3yr F&O history
          </span>
        </div>
        <div className="flex items-center gap-1">
          {status?.connected && (
            <button onClick={() => disconnectBroker("icici_breeze").then(onRefresh)}
              className="text-text-muted hover:text-accent-red p-1 rounded"><Unplug size={14} /></button>
          )}
          <button onClick={() => setExpanded(!expanded)} className="text-text-muted hover:text-text-primary p-1">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>

      {expanded && (
        <div className="space-y-4 pt-2 border-t border-bg-border">
          <div className="bg-accent-amber/5 border border-accent-amber/20 rounded p-3 space-y-1 text-xs">
            <div className="font-bold text-accent-amber">Breeze API credentials (not your banking login)</div>
            <p className="text-text-muted">
              Get API Key + Secret from{" "}
              <a href="https://api.icicidirect.com" target="_blank" rel="noopener noreferrer"
                className="text-accent-blue hover:underline">api.icicidirect.com</a>
              {" "}→ <strong className="text-text-primary">My Apps</strong> → create an app if none exists → use the app's{" "}
              <strong className="text-text-primary">App Key</strong> (not client ID) and{" "}
              <strong className="text-text-primary">Secret Key</strong>.
            </p>
            <p className="text-accent-red/80 text-xs">⚠ "Public key does not exist" = wrong App Key — re-copy from My Apps.</p>
          </div>

          {hasCreds && (
            <div className="bg-accent-blue/5 border border-accent-blue/20 rounded p-2 text-xs text-accent-blue flex items-center gap-2">
              <CheckCircle2 size={12} /> Credentials saved — skip Step 1 and go to login.
            </div>
          )}

          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 1 — API Credentials (one-time)</div>
            <TextInput value={apiKey} onChange={setApiKey} label="API Key (from api.icicidirect.com)" placeholder="J279)$5v…" saved={savedFields["api_key"]} />
            <PasswordInput value={secret} onChange={setSecret} label="Secret Key" placeholder="xi3j020b…" saved={savedFields["secret"]} />
            <button onClick={handleSaveCreds} disabled={saving}
              className="px-3 py-1.5 rounded text-xs bg-bg-hover border border-bg-border text-text-secondary hover:border-accent-red/40 disabled:opacity-50 flex items-center gap-1">
              {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Credentials
            </button>
          </div>

          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 2 — Login to ICICI Direct</div>
            <div className="bg-accent-red/8 border border-accent-red/25 rounded p-2 text-xs text-accent-red flex items-start gap-1.5">
              <AlertCircle size={11} className="mt-0.5 shrink-0" />
              <span><strong>Token expires in ~30 seconds!</strong> After the redirect, immediately copy the token and paste it below — do not navigate away or wait.</span>
            </div>
            <div className="text-xs text-text-muted space-y-1">
              <p>1. Click the button below → ICICI Direct login page opens.</p>
              <p>2. Log in with your ICICI Direct username and password.</p>
              <p>3. After login you'll be redirected to your app's Redirect URL (e.g. gooogle.com).</p>
              <p>4. Copy the <code className="text-accent-green bg-bg-secondary px-1 rounded">apisession=</code> value from the URL bar — it may look like 8 digits or a longer string.</p>
              <p>5. Immediately paste it in Step 3 below and click Connect (within 30 seconds).</p>
            </div>
            <button onClick={handleGetLoginUrl} disabled={saving}
              className="w-full py-2 rounded text-sm bg-accent-red/15 border border-accent-red/30 text-accent-red hover:bg-accent-red/25 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <ExternalLink size={14} />} Open ICICI Direct Login
            </button>
          </div>

          <div className="space-y-2">
            <div className="text-xs font-bold text-text-secondary">Step 3 — Paste Session Token</div>
            <p className="text-xs text-text-muted">
              From URL: <code className="text-accent-green bg-bg-secondary px-1 rounded">https://gooogle.com/?apisession=<strong>TOKEN_HERE</strong></code>
            </p>
            <PasswordInput value={sessionToken}
              onChange={(v) => {
                setSessionToken(v);
                setTokenAge(0);
                if (tokenAgeRef.current) clearInterval(tokenAgeRef.current);
                if (v) {
                  tokenAgeRef.current = setInterval(() => setTokenAge(a => a + 1), 1000);
                }
              }}
              label="apisession token (from redirect URL)" placeholder="paste the token here" />
            {sessionToken && tokenAge > 0 && (
              <p className={clsx("text-xs font-mono", tokenAge >= 25 ? "text-accent-red animate-pulse" : tokenAge >= 15 ? "text-accent-amber" : "text-accent-green")}>
                ⏱ Token age: {tokenAge}s{tokenAge >= 30 ? " — EXPIRED, get a fresh token!" : tokenAge >= 15 ? " — connect now!" : " — good"}
              </p>
            )}
            <button onClick={handleConnect} disabled={saving || !sessionToken}
              className="w-full py-2 rounded text-sm bg-accent-red/20 border border-accent-red/30 text-accent-red hover:bg-accent-red/30 disabled:opacity-50 flex items-center justify-center gap-2">
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect ICICI Breeze
            </button>
          </div>
          {msg && <p className={clsx("text-xs", msg.startsWith("✓") ? "text-accent-green" : msg.startsWith("Login") ? "text-accent-amber" : "text-accent-red")}>{msg}</p>}
        </div>
      )}
    </div>
  );
}

// ── Main Settings Page ────────────────────────────────────────────────────────

export default function SettingsPage() {
  const qc = useQueryClient();

  const { data: brokerStatuses, refetch: refetchBrokers } = useQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then(r => r.data),
    refetchInterval: 30000,
    staleTime: 15000,
  });

  const { data: riskData } = useQuery({
    queryKey: ["riskStatus"],
    queryFn: () => getRiskStatus().then(r => r.data),
    staleTime: 30000,
  });

  const [maxLoss, setMaxLoss] = useState("");
  const [dailyLoss, setDailyLoss] = useState("");
  const [maxPos, setMaxPos] = useState("");

  const riskMut = useMutation({
    mutationFn: updateRiskConfig,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["riskStatus"] }),
  });

  const statusMap = Object.fromEntries((brokerStatuses || []).map((s: any) => [s.broker, s]));

  const handleRefresh = useCallback(() => {
    refetchBrokers();
    qc.invalidateQueries({ queryKey: ["brokerStatus"] });
    qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
  }, [refetchBrokers, qc]);

  return (
    <div className="max-w-2xl space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold font-mono text-text-primary">Settings</h1>
        <button onClick={handleRefresh} className="text-text-muted hover:text-text-primary p-1 rounded" title="Refresh">
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Broker Connections */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">Broker Connections</h2>
        <p className="text-xs text-text-muted">
          Credentials and access tokens are saved permanently to disk.
          Upstox auto-reconnects on restart (JWT token is long-lived). Other brokers require re-auth each session.
        </p>
        <UpstoxApiBudgetCard />
        <TelegramCard />
        <FyersCard status={statusMap["fyers"]} onRefresh={handleRefresh} />
        <UpstoxCard status={statusMap["upstox"]} onRefresh={handleRefresh} />
        <FivePaisaCard status={statusMap["fivepaisa"]} onRefresh={handleRefresh} />
        <ICICIBreezeCard status={statusMap["icici_breeze"]} onRefresh={handleRefresh} />
      </section>

      {/* Risk Controls */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wide">Risk Controls</h2>
        {riskData && (
          <div className="card p-4 space-y-3">
            <div className="grid grid-cols-3 gap-3">
              <TextInput value={maxLoss || riskData?.max_loss_pct?.toString() || ""}
                onChange={setMaxLoss} label="Max Loss %" placeholder="5" />
              <TextInput value={dailyLoss || riskData?.daily_loss_limit?.toString() || ""}
                onChange={setDailyLoss} label="Daily Loss ₹" placeholder="10000" />
              <TextInput value={maxPos || riskData?.max_positions?.toString() || ""}
                onChange={setMaxPos} label="Max Positions" placeholder="5" />
            </div>
            <button
              onClick={() => riskMut.mutate({ max_loss_pct: +maxLoss, daily_loss_limit: +dailyLoss, max_positions: +maxPos })}
              disabled={riskMut.isPending}
              className="px-3 py-1.5 rounded text-xs bg-accent-blue/15 border border-accent-blue/30 text-accent-blue hover:bg-accent-blue/25 disabled:opacity-50 flex items-center gap-1">
              {riskMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Update Risk Config
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

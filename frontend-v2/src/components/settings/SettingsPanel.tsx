"use client";

/**
 * Settings — native v2 surface.
 *
 * Replaces the v1 settings embed. Form-heavy operator console:
 *   • Broker connections   (Fyers / Upstox / 5Paisa / ICICI Breeze)
 *       status   → /api/auth/broker-status (+ all-credentials-status)
 *       save     → /api/auth/save-credentials
 *       connect  → per-broker OAuth / TOTP / session flows
 *   • Telegram notifications → /api/auth/telegram-settings
 *   • Trading calendar gate  → /api/system/trading-calendar
 *   • Risk controls          → /api/trading/risk-status (+ risk-config PUT)
 *
 * Design system: header + KPI strip + Section blocks (ProposalsBoard idiom),
 * desk-ui primitives, theme tokens only. Inputs reuse the app `terminal-input`
 * class. Mutations use react-query + invalidateQueries; partial saves never
 * wipe unedited (saved) fields.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Copy,
  ExternalLink,
  Eye,
  EyeOff,
  Info,
  Link2,
  Loader2,
  Plug,
  RefreshCw,
  Save,
  Send,
  ShieldCheck,
  Unplug,
  XCircle,
} from "lucide-react";
import { clsx } from "clsx";

import {
  MetricTile,
  REFRESH_MS,
  Section,
  StatusBadge,
  formatIST,
  formatMoney,
  serviceStateTone,
  tone,
} from "@/components/desk-ui";
import {
  api,
  connectBroker,
  connectFivepaisa,
  connectIciciBreeze,
  connectUpstox,
  describeApiError,
  disconnectBroker,
  getBrokerStatus,
  getCredentialsStatus,
  getFyersAuthUrl,
  getIciciLoginUrl,
  getRiskStatus,
  getTelegramSettings,
  getTradingCalendar,
  getUpstoxAuthUrl,
  saveCredentials,
  saveTelegramSettings,
  discoverTelegramChats,
  sendTelegramTest,
  updateRiskConfig,
  updateTradingCalendar,
} from "@/lib/api";
import {
  hasBrokerSession,
  isBrokerReady,
  type BrokerStatusEntry,
} from "@/lib/broker-status";
import { useStore } from "@/store";

const V1_HREF = "http://localhost:3000/settings";
const FYERS_FIXED_REDIRECT_URI = "https://trade.fyers.in/api-login/redirect-uri/index.html";
const UPSTOX_REDIRECT = "https://www.google.com";

// ── Field-level primitives ───────────────────────────────────────────────────

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="mb-1 block text-[11px] text-text-muted">{children}</label>;
}

function TextField({
  value,
  onChange,
  placeholder,
  label,
  readOnly,
  saved,
}: {
  value: string;
  onChange?: (v: string) => void;
  placeholder?: string;
  label?: string;
  readOnly?: boolean;
  saved?: boolean;
}) {
  return (
    <div>
      {label ? <FieldLabel>{label}</FieldLabel> : null}
      <input
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        placeholder={saved && !value ? "•••••  saved" : placeholder}
        readOnly={readOnly}
        spellCheck={false}
        className={clsx(
          "terminal-input w-full text-sm",
          readOnly && "cursor-default opacity-70",
          saved && !value && "border-l-2 border-l-accent-green placeholder:text-accent-green",
        )}
      />
    </div>
  );
}

function PasswordField({
  value,
  onChange,
  placeholder,
  label,
  saved,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  label?: string;
  saved?: boolean;
}) {
  const [show, setShow] = useState(false);
  return (
    <div>
      {label ? <FieldLabel>{label}</FieldLabel> : null}
      <div className="relative">
        <input
          type={show ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={saved && !value ? "•••••  saved" : placeholder}
          spellCheck={false}
          className={clsx(
            "terminal-input w-full pr-8 text-sm",
            saved && !value && "border-l-2 border-l-accent-green placeholder:text-accent-green",
          )}
        />
        <button
          type="button"
          onClick={() => setShow((s) => !s)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text-secondary"
          tabIndex={-1}
        >
          {show ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
    </div>
  );
}

function CopyableUrl({ url, label }: { url: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div>
      {label ? <FieldLabel>{label}</FieldLabel> : null}
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded border border-bg-border bg-bg-primary/30 px-2 py-1.5 font-mono text-[11.5px] text-accent-green">
          {url}
        </code>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard?.writeText(url);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          }}
          className="shrink-0 rounded p-1 text-text-muted hover:text-text-primary"
          title="Copy"
        >
          {copied ? <CheckCircle2 size={13} className="text-accent-green" /> : <Copy size={13} />}
        </button>
      </div>
    </div>
  );
}

function Feedback({ msg }: { msg: string }) {
  if (!msg) return null;
  const ok = msg.startsWith("✓");
  const info = msg.startsWith("Login") || msg.startsWith("Connecting");
  return (
    <p
      className={clsx(
        "whitespace-pre-wrap text-[12px]",
        ok ? "text-accent-green" : info ? "text-accent-amber" : "text-accent-red",
      )}
    >
      {msg}
    </p>
  );
}

// ── Shared button styles ──────────────────────────────────────────────────────

const SAVE_BTN =
  "inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-1.5 text-[12px] font-semibold text-text-secondary hover:border-bg-active hover:text-text-primary disabled:opacity-50";
const PRIMARY_BTN =
  "inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent-blue/35 bg-accent-blue/12 py-2 text-[13px] font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50";

// ── Broker badge helpers ──────────────────────────────────────────────────────

function brokerBadgeVariant(status?: Partial<BrokerStatusEntry> | null): "success" | "warn" | "neutral" {
  if (isBrokerReady(status)) return "success";
  if (status?.needs_reconnect) return "warn";
  return "neutral";
}
function brokerBadgeLabel(status?: Partial<BrokerStatusEntry> | null): string {
  if (isBrokerReady(status)) return "Connected";
  if (status?.needs_reconnect) return "Reconnect";
  return "Disconnected";
}
function brokerBadgeIcon(status?: Partial<BrokerStatusEntry> | null): React.ReactNode {
  if (isBrokerReady(status)) return <CheckCircle2 size={11} />;
  if (status?.needs_reconnect) return <AlertCircle size={11} />;
  return <XCircle size={11} />;
}

function humanize(value?: string | null): string | null {
  const v = String(value || "").trim();
  return v ? v.replace(/_/g, " ") : null;
}
function checkedAtLabel(value?: string | null): string | null {
  if (!value) return null;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat("en-IN", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "Asia/Kolkata",
  }).format(d);
}
function brokerMeta(status?: Partial<BrokerStatusEntry> | null): string | null {
  if (!status) return null;
  const at = checkedAtLabel(status.checked_at);
  const src = humanize(status.source);
  if (isBrokerReady(status)) {
    const via = src ? `via ${src}` : "via broker validation";
    return at ? `Verified ${via} · ${at}` : `Verified ${via}`;
  }
  const parts = [humanize(status.state), src, at ? `checked ${at}` : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function mergeBrokerStatuses(
  primary?: BrokerStatusEntry[],
  fallback?: BrokerStatusEntry[],
): BrokerStatusEntry[] {
  const merged = new Map<string, BrokerStatusEntry>();
  for (const s of fallback || []) merged.set(s.broker, s);
  for (const s of primary || []) merged.set(s.broker, { ...(merged.get(s.broker) || {}), ...s });
  return Array.from(merged.values());
}

function CredsSavedChip({ fields }: { fields: Record<string, boolean> }) {
  const total = Object.keys(fields).length;
  const filled = Object.values(fields).filter(Boolean).length;
  if (!filled) return null;
  const all = total > 0 && filled === total;
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em]",
        all
          ? "border-accent-blue/30 bg-accent-blue/10 text-accent-blue"
          : "border-accent-amber/30 bg-accent-amber/10 text-accent-amber",
      )}
    >
      <Save size={9} />
      {all ? "Creds saved" : `${filled}/${total} saved`}
    </span>
  );
}

function useAllCredsStatus() {
  return useQuery({
    queryKey: ["allCredsStatus"],
    queryFn: () => api.get("/api/auth/all-credentials-status").then((r) => r.data),
    staleTime: 300_000,
    gcTime: 900_000,
    refetchOnWindowFocus: false,
    retry: 2,
    retryDelay: (attempt) => Math.min(1000 * (attempt + 1), 3000),
  });
}

// ── Broker card shell ─────────────────────────────────────────────────────────

function BrokerCard({
  badge,
  badgeTone,
  name,
  subtitle,
  status,
  tags,
  credsChip,
  onDisconnect,
  expanded,
  setExpanded,
  children,
}: {
  badge: string;
  badgeTone: string;
  name: React.ReactNode;
  subtitle?: React.ReactNode;
  status?: BrokerStatusEntry;
  tags?: React.ReactNode;
  credsChip?: React.ReactNode;
  onDisconnect?: () => void;
  expanded: boolean;
  setExpanded: (v: boolean) => void;
  children: React.ReactNode;
}) {
  const ready = isBrokerReady(status);
  const sessionActive = hasBrokerSession(status);
  const meta = brokerMeta(status);
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div
            className={clsx(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border text-[11px] font-bold",
              badgeTone,
            )}
          >
            {badge}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-text-primary">{name}</span>
              <StatusBadge
                label={brokerBadgeLabel(status)}
                variant={brokerBadgeVariant(status)}
                icon={brokerBadgeIcon(status)}
              />
              {!ready && credsChip}
              {tags}
            </div>
            {ready && status?.name ? (
              <div className="mt-0.5 text-[11px] text-text-muted">
                {status.name}
                {status.user_id ? ` · ${status.user_id}` : ""}
              </div>
            ) : subtitle ? (
              <div className="mt-0.5 text-[11px] text-text-muted">{subtitle}</div>
            ) : null}
            {meta ? <div className="mt-0.5 text-[10.5px] text-text-muted">{meta}</div> : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {sessionActive && onDisconnect ? (
            <button
              type="button"
              onClick={onDisconnect}
              className="rounded p-1 text-text-muted hover:text-accent-red"
              title="Disconnect"
            >
              <Unplug size={15} />
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="rounded p-1 text-text-muted hover:text-text-primary"
            title={expanded ? "Collapse" : "Expand"}
          >
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>
        </div>
      </div>
      {expanded ? <div className="mt-3 space-y-4 border-t border-bg-border/70 pt-3">{children}</div> : null}
    </div>
  );
}

function StepHeading({ children }: { children: React.ReactNode }) {
  return <div className="text-[11.5px] font-bold uppercase tracking-[0.08em] text-text-secondary">{children}</div>;
}

function InfoNote({
  tone: t = "blue",
  icon,
  children,
}: {
  tone?: "blue" | "amber" | "green" | "red";
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  const cls = {
    blue: "border-accent-blue/25 bg-accent-blue/5 text-accent-blue",
    amber: "border-accent-amber/25 bg-accent-amber/5 text-accent-amber",
    green: "border-accent-green/25 bg-accent-green/5 text-accent-green",
    red: "border-accent-red/25 bg-accent-red/5 text-accent-red",
  }[t];
  return (
    <div className={clsx("flex items-start gap-2 rounded-lg border px-3 py-2 text-[11.5px]", cls)}>
      {icon ? <span className="mt-0.5 shrink-0">{icon}</span> : null}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

// ── Fyers ─────────────────────────────────────────────────────────────────────

function FyersCard({ status, onRefresh }: { status?: BrokerStatusEntry; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.fyers?.fields ?? {};
  const hasCreds = Boolean(allCreds?.fyers?.has_credentials);
  const ready = isBrokerReady(status);

  const [expanded, setExpanded] = useState(false);
  const { data: fyersCreds } = useQuery({
    queryKey: ["credentialsStatus", "fyers"],
    queryFn: () => getCredentialsStatus("fyers").then((r) => r.data),
    staleTime: 15_000,
    enabled: expanded,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const [appId, setAppId] = useState("");
  const [secret, setSecret] = useState("");
  const [pin, setPin] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const savedAppId = String(fyersCreds?.display?.app_id || "").trim();
  const redirectUri = String(fyersCreds?.display?.redirect_uri || FYERS_FIXED_REDIRECT_URI);

  useEffect(() => {
    if (fyersCreds?.display?.app_id && !appId) setAppId(String(fyersCreds.display.app_id));
  }, [fyersCreds, appId]);

  useEffect(() => {
    const h = (e: MessageEvent) => {
      if (e.data?.broker === "fyers" && e.data?.status === "connected") {
        setExpanded(false);
        onRefresh();
      }
    };
    window.addEventListener("message", h);
    return () => window.removeEventListener("message", h);
  }, [onRefresh]);

  const handleSave = async () => {
    const appIdToSave = appId.trim() || savedAppId;
    const secretToSave = secret.trim();
    const pinToSave = pin.trim();
    if (!appIdToSave) return setMsg("Enter APP ID");
    if (!secretToSave && !savedFields["secret"]) return setMsg("Enter Secret Key");
    setSaving(true);
    setMsg("");
    try {
      await saveCredentials("fyers", {
        app_id: appIdToSave,
        ...(secretToSave ? { secret: secretToSave } : {}),
        ...(pinToSave ? { pin: pinToSave } : {}),
        redirect_uri: FYERS_FIXED_REDIRECT_URI,
      });
      setSecret("");
      setPin("");
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
      qc.invalidateQueries({ queryKey: ["credentialsStatus", "fyers"] });
    } catch (e) {
      setMsg(describeApiError(e, "Failed to save Fyers credentials"));
    } finally {
      setSaving(false);
    }
  };

  const handleLogin = async () => {
    const w = window.open("", "fyers-login", "width=600,height=700");
    try {
      const r = await getFyersAuthUrl();
      const url = String(r.data.auth_url || "");
      if (!url) {
        w?.close();
        return setMsg("Fyers did not return a login URL.");
      }
      if (w) {
        w.location.href = url;
        w.focus();
      } else {
        window.location.assign(url);
      }
      setMsg("Login page opened. After login, copy the auth_code from the redirect URL and paste it below.");
    } catch (e) {
      w?.close();
      setMsg(describeApiError(e, "Save credentials first"));
    }
  };

  const handleConnect = async () => {
    if (!authCode.trim()) return setMsg("Paste the auth_code from the Fyers redirect page");
    setSaving(true);
    setMsg("");
    try {
      await connectBroker("fyers", { auth_code: authCode.trim() });
      setMsg("✓ Connected!");
      setExpanded(false);
      onRefresh();
    } catch (e) {
      setMsg(describeApiError(e, "Fyers connection failed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <BrokerCard
      badge="FY"
      badgeTone="border-accent-amber/30 bg-accent-amber/12 text-accent-amber"
      name="Fyers"
      status={status}
      credsChip={<CredsSavedChip fields={savedFields} />}
      onDisconnect={() => disconnectBroker("fyers").then(onRefresh)}
      expanded={expanded}
      setExpanded={setExpanded}
    >
      <InfoNote tone="amber" icon={<Info size={12} />}>
        <div className="font-bold">Fyers uses this fixed redirect URL</div>
        <div className="mt-1.5">
          <CopyableUrl url={redirectUri} label="Saved Redirect URL (register in myapi.fyers.in → App Settings)" />
        </div>
        <p className="mt-1 text-[11px] text-text-muted">
          Go to{" "}
          <a href="https://myapi.fyers.in" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent-blue hover:underline">
            myapi.fyers.in <ExternalLink size={9} />
          </a>{" "}
          → My Apps → Edit app → set this redirect URL once → Save.
        </p>
      </InfoNote>

      {hasCreds ? <InfoNote tone="green" icon={<CheckCircle2 size={12} />}>Credentials saved — go to Step 2 to login.</InfoNote> : null}
      {status?.detail && !ready ? <InfoNote tone="amber" icon={<AlertCircle size={12} />}>{status.detail}</InfoNote> : null}
      {savedFields["access_token"] ? (
        <InfoNote tone="green" icon={<CheckCircle2 size={12} />}>Saved Fyers session found — reused until Fyers expires it.</InfoNote>
      ) : null}

      <div className="space-y-2">
        <StepHeading>Step 1 — API Credentials</StepHeading>
        <TextField value={appId} onChange={setAppId} label="APP ID (Client ID)" placeholder="XXXXXXXX-100" saved={savedFields["app_id"]} />
        <PasswordField value={secret} onChange={setSecret} label="Secret Key" placeholder="secret" saved={savedFields["secret"]} />
        <PasswordField value={pin} onChange={setPin} label="PIN for refresh-token reuse" placeholder="optional; stored encrypted" saved={savedFields["pin"]} />
        <TextField value={redirectUri} label="Redirect URI (fixed for Fyers login)" readOnly saved={savedFields["redirect_uri"]} />
        <button type="button" onClick={handleSave} disabled={saving} className={SAVE_BTN}>
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save Credentials
        </button>
      </div>

      <div className="space-y-2">
        <StepHeading>Step 2 — Authorize</StepHeading>
        <button
          type="button"
          onClick={handleLogin}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent-amber/35 bg-accent-amber/12 py-2 text-[13px] font-semibold text-accent-amber hover:bg-accent-amber/20"
        >
          <ExternalLink size={14} /> Open Fyers Login Page
        </button>
        <p className="text-[11px] text-text-muted">
          After login you&apos;ll land on{" "}
          <code className="rounded bg-bg-primary/40 px-1 text-accent-green">…?auth_code=XXXX</code>. Copy the auth_code value.
        </p>
        <PasswordField value={authCode} onChange={setAuthCode} label="Auth Code from redirect URL" placeholder="paste auth_code here" />
        <button type="button" onClick={handleConnect} disabled={saving || !authCode.trim()} className={PRIMARY_BTN}>
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect Fyers
        </button>
      </div>
      <Feedback msg={msg} />
    </BrokerCard>
  );
}

// ── Upstox ────────────────────────────────────────────────────────────────────

function UpstoxCard({ status, onRefresh }: { status?: BrokerStatusEntry; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.upstox?.fields ?? {};
  const hasCreds = Boolean(allCreds?.upstox?.has_credentials);
  const ready = isBrokerReady(status);

  const [expanded, setExpanded] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [secret, setSecret] = useState("");
  const [analyticsToken, setAnalyticsToken] = useState("");
  const [authCode, setAuthCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  const PLACEHOLDERS = useMemo(
    () => new Set(["x", "xx", "xxx", "test", "dummy", "placeholder", "your_api_key", "your_secret"]),
    [],
  );

  const handleSave = async () => {
    const k = apiKey.trim();
    const s = secret.trim();
    const at = analyticsToken.trim();
    if (k && PLACEHOLDERS.has(k.toLowerCase())) return setMsg("Enter the real Upstox API Key — not a placeholder like x.");
    if (s && PLACEHOLDERS.has(s.toLowerCase())) return setMsg("Enter the real Upstox Secret — not a placeholder like x.");
    if (!at && !k && !savedFields["api_key"]) return setMsg("Enter API Key or Analytics Token");
    if (!at && !s && !savedFields["secret"]) return setMsg("Enter Secret or Analytics Token");
    setSaving(true);
    setMsg("");
    try {
      await saveCredentials("upstox", {
        ...(k ? { api_key: k } : {}),
        ...(s ? { secret: s } : {}),
        ...(at ? { analytics_token: at } : {}),
        redirect_uri: UPSTOX_REDIRECT,
      });
      setSecret("");
      setAnalyticsToken("");
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e) {
      setMsg(describeApiError(e, "Failed to save Upstox credentials"));
    } finally {
      setSaving(false);
    }
  };

  const handleLogin = async () => {
    setSaving(true);
    setMsg("");
    try {
      const r = await getUpstoxAuthUrl();
      window.open(r.data.auth_url, "_blank");
      setMsg("Login page opened. After login Google opens — copy the code= value from the URL and paste in Step 3.");
    } catch (e) {
      setMsg(describeApiError(e, "Save credentials first"));
    } finally {
      setSaving(false);
    }
  };

  const handleConnect = async () => {
    const code = authCode.trim();
    if (!code) return setMsg("Paste the authorization code or access token");
    setSaving(true);
    setMsg("Connecting…");
    try {
      await connectUpstox(code);
      setMsg("✓ Connected!");
      setExpanded(false);
      onRefresh();
    } catch (e) {
      const detail = describeApiError(e, "Connection failed — check backend logs");
      const d = detail.toLowerCase();
      if (d.includes("udapi100068") || d.includes("redirect_uri")) {
        setMsg("Redirect URI mismatch — set your Upstox app Redirect URL to https://www.google.com");
      } else if (d.includes("expired") || (d.includes("invalid") && !d.includes("token exchange"))) {
        setMsg("⏰ Code expired — codes are single-use. Open Login again and paste the fresh code immediately.");
      } else {
        setMsg(detail);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <BrokerCard
      badge="UP"
      badgeTone="border-accent-blue/30 bg-accent-blue/12 text-accent-blue"
      name="Upstox"
      status={status}
      credsChip={<CredsSavedChip fields={savedFields} />}
      onDisconnect={() => disconnectBroker("upstox").then(onRefresh)}
      tags={
        <>
          <StatusBadge label="1yr F&O" variant="info" />
          <StatusBadge label="Sandbox" variant="neutral" />
        </>
      }
      expanded={expanded}
      setExpanded={setExpanded}
    >
      <InfoNote tone="blue" icon={<Info size={12} />}>
        <div className="font-bold">Sandbox App — Manual Code Flow</div>
        <p className="mt-0.5 text-[11px] text-text-muted">
          Sandbox apps redirect to <code className="rounded bg-bg-primary/40 px-1 text-accent-green">https://www.google.com</code>. After
          login, copy the <code className="rounded bg-bg-primary/40 px-1 text-accent-green">code=</code> value from Google&apos;s URL bar.
        </p>
      </InfoNote>

      {hasCreds ? <InfoNote tone="green" icon={<CheckCircle2 size={12} />}>Credentials saved — proceed to Step 2.</InfoNote> : null}
      {status?.detail && !ready ? <InfoNote tone="amber" icon={<AlertCircle size={12} />}>{status.detail}</InfoNote> : null}

      <div className="space-y-2">
        <StepHeading>Step 1 — API Credentials (one-time)</StepHeading>
        <p className="text-[11px] text-text-muted">
          From{" "}
          <a href="https://account.upstox.com/developer/apps" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent-blue hover:underline">
            account.upstox.com → Apps <ExternalLink size={9} />
          </a>{" "}
          → your Sandbox App → API Key and Secret.
        </p>
        <TextField value={apiKey} onChange={setApiKey} label="API Key" placeholder="your_api_key" saved={savedFields["api_key"]} />
        <PasswordField value={secret} onChange={setSecret} label="Secret" placeholder="your_secret" saved={savedFields["secret"]} />
        <PasswordField value={analyticsToken} onChange={setAnalyticsToken} label="Analytics Token (paper / backfill)" placeholder="optional long-lived read-only token" saved={savedFields["analytics_token"]} />
        <button type="button" onClick={handleSave} disabled={saving} className={SAVE_BTN}>
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save Credentials
        </button>
      </div>

      <div className="space-y-2">
        <StepHeading>Step 2 — Open Upstox Login</StepHeading>
        <p className="text-[11px] text-text-muted">
          Click below → log in → you&apos;re redirected to{" "}
          <span className="font-mono text-accent-green">google.com/?code=<strong>AUTH_CODE</strong>&amp;…</span>. Copy everything after{" "}
          <code className="text-accent-green">code=</code> and before <code className="text-accent-green">&amp;</code>.
        </p>
        <button type="button" onClick={handleLogin} disabled={saving} className={PRIMARY_BTN}>
          {saving ? <Loader2 size={14} className="animate-spin" /> : <ExternalLink size={14} />} Open Upstox Login
        </button>
      </div>

      <div className="space-y-2 rounded-lg border border-bg-border bg-bg-primary/20 p-3">
        <StepHeading>Step 3 — Paste Code or Access Token</StepHeading>
        <InfoNote tone="amber" icon={<AlertCircle size={11} />}>Auth codes are single-use and expire in ~1 minute. Connect immediately after pasting.</InfoNote>
        <TextField
          value={authCode}
          onChange={setAuthCode}
          label="Auth code (from Google URL) or existing access token (eyJ…)"
          placeholder="paste code or eyJ… token here"
          saved={savedFields["access_token"]}
        />
        {savedFields["access_token"] && !ready ? (
          <p className="flex items-center gap-1 text-[11px] text-accent-green/80">
            <CheckCircle2 size={11} /> Saved access token found — auto-connects on next restart, or paste a fresh token and Connect now.
          </p>
        ) : null}
        <button type="button" onClick={handleConnect} disabled={saving || !authCode.trim()} className={PRIMARY_BTN}>
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect Upstox
        </button>
      </div>
      <Feedback msg={msg} />
    </BrokerCard>
  );
}

// ── 5Paisa ────────────────────────────────────────────────────────────────────

const FIVEPAISA_FIELDS: Array<{ key: keyof FivePaisaForm; label: string; placeholder: string; secret?: boolean }> = [
  { key: "app_name", label: "App Name (App ID)", placeholder: "5P56340816" },
  { key: "app_source", label: "App Source", placeholder: "9918" },
  { key: "user_id", label: "User ID (Client Code)", placeholder: "NL0BYabni01" },
  { key: "email", label: "Registered Email", placeholder: "you@example.com" },
  { key: "password", label: "User Password", placeholder: "password", secret: true },
  { key: "user_key", label: "User Key (= API Key)", placeholder: "sQzETAqIr2…", secret: true },
  { key: "encryption_key", label: "Encryption Key", placeholder: "qK5i7sME…", secret: true },
];

type FivePaisaForm = {
  app_name: string;
  app_source: string;
  user_id: string;
  email: string;
  password: string;
  user_key: string;
  encryption_key: string;
};

function FivePaisaCard({ status, onRefresh }: { status?: BrokerStatusEntry; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.fivepaisa?.fields ?? {};
  const hasCreds = Boolean(allCreds?.fivepaisa?.has_credentials);
  const allFieldsSaved = hasCreds && Object.values(savedFields).filter(Boolean).length >= 7;
  const ready = isBrokerReady(status);

  const [expanded, setExpanded] = useState(false);
  const [fields, setFields] = useState<FivePaisaForm>({
    app_name: "",
    app_source: "",
    user_id: "",
    email: "",
    password: "",
    user_key: "",
    encryption_key: "",
  });
  const [totp, setTotp] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (allFieldsSaved && !ready) setExpanded(true);
  }, [allFieldsSaved, ready]);

  const setF = (k: keyof FivePaisaForm) => (v: string) => setFields((f) => ({ ...f, [k]: v }));

  const handleSave = async () => {
    const toSave = Object.fromEntries(Object.entries(fields).filter(([, v]) => v));
    if (Object.keys(toSave).length === 0) return setMsg("Fill at least one field");
    setSaving(true);
    setMsg("");
    try {
      await saveCredentials("fivepaisa", toSave as Record<string, string>);
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e) {
      setMsg(describeApiError(e, "Failed to save 5Paisa credentials"));
    } finally {
      setSaving(false);
    }
  };

  const handleConnect = async () => {
    const t = totp.trim();
    if (t.length !== 6) return setMsg("Enter the 6-digit TOTP from your authenticator app");
    setSaving(true);
    setMsg("Connecting…");
    try {
      await connectFivepaisa(t);
      setMsg("✓ Connected!");
      setExpanded(false);
      onRefresh();
    } catch (e) {
      const detail = describeApiError(e, "Connection failed");
      setMsg(detail.toLowerCase().includes("totp") || detail.toLowerCase().includes("otp") ? `TOTP error — check your authenticator app: ${detail}` : detail);
    } finally {
      setSaving(false);
    }
  };

  return (
    <BrokerCard
      badge="5P"
      badgeTone="border-accent-green/30 bg-accent-green/12 text-accent-green"
      name="5Paisa"
      status={status}
      credsChip={<CredsSavedChip fields={savedFields} />}
      onDisconnect={() => disconnectBroker("fivepaisa").then(onRefresh)}
      expanded={expanded}
      setExpanded={setExpanded}
    >
      <p className="text-[11px] text-text-muted">
        Credentials from{" "}
        <a href="https://xstream.5paisa.com" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent-blue hover:underline">
          xstream.5paisa.com <ExternalLink size={10} />
        </a>{" "}
        → Dashboard. <strong className="text-text-secondary">User Key = API Key field</strong> (not Encryption Key).
      </p>

      {allFieldsSaved ? (
        <InfoNote tone="green" icon={<CheckCircle2 size={12} />}>All 7 API credentials saved. Enter TOTP below to connect.</InfoNote>
      ) : (
        <div className="space-y-2">
          <StepHeading>
            Step 1 — API Credentials{hasCreds ? <span className="ml-2 font-normal text-accent-amber">(partially saved)</span> : null}
          </StepHeading>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {FIVEPAISA_FIELDS.map((f) =>
              f.secret ? (
                <PasswordField key={f.key} value={fields[f.key]} onChange={setF(f.key)} label={f.label} placeholder={f.placeholder} saved={savedFields[f.key]} />
              ) : (
                <TextField key={f.key} value={fields[f.key]} onChange={setF(f.key)} label={f.label} placeholder={f.placeholder} saved={savedFields[f.key]} />
              ),
            )}
          </div>
          <button type="button" onClick={handleSave} disabled={saving} className={SAVE_BTN}>
            {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save Credentials
          </button>
        </div>
      )}

      <div className="space-y-2 rounded-lg border border-bg-border bg-bg-primary/20 p-3">
        <StepHeading>{allFieldsSaved ? "Connect with TOTP" : "Step 2 — Connect with TOTP"}</StepHeading>
        <InfoNote tone="amber" icon={<AlertCircle size={11} />}>TOTP must be enabled on your 5Paisa account. Codes expire in 30 seconds.</InfoNote>
        <input
          value={totp}
          onChange={(e) => setTotp(e.target.value.replace(/\D/g, "").slice(0, 6))}
          placeholder="Enter 6-digit TOTP"
          maxLength={6}
          inputMode="numeric"
          className="terminal-input w-full text-center text-lg font-mono tracking-[0.3em]"
        />
        <button
          type="button"
          onClick={handleConnect}
          disabled={saving || totp.trim().length !== 6}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent-green/35 bg-accent-green/12 py-2 text-[13px] font-semibold text-accent-green hover:bg-accent-green/20 disabled:opacity-50"
        >
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />}
          {saving ? "Connecting…" : "Connect 5Paisa"}
        </button>
        {totp.length > 0 && totp.length < 6 ? <p className="text-center text-[11px] text-text-muted">{6 - totp.length} more digits needed</p> : null}
      </div>
      <Feedback msg={msg} />
    </BrokerCard>
  );
}

// ── ICICI Breeze ──────────────────────────────────────────────────────────────

function ICICIBreezeCard({ status, onRefresh }: { status?: BrokerStatusEntry; onRefresh: () => void }) {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = allCreds?.icici_breeze?.fields ?? {};
  const hasCreds = Boolean(allCreds?.icici_breeze?.has_credentials);
  const ready = isBrokerReady(status);

  const [expanded, setExpanded] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [secret, setSecret] = useState("");
  const [sessionToken, setSessionToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [tokenAge, setTokenAge] = useState(0);
  const tokenAgeRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    if (tokenAgeRef.current) clearInterval(tokenAgeRef.current);
  }, []);

  const handleSave = async () => {
    if (!apiKey && !secret) return setMsg("Enter at least one field");
    setSaving(true);
    setMsg("");
    try {
      const toSave: Record<string, string> = {};
      if (apiKey) toSave.api_key = apiKey;
      if (secret) toSave.secret = secret;
      await saveCredentials("icici_breeze", toSave);
      setMsg("✓ Credentials saved");
      qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    } catch (e) {
      setMsg(describeApiError(e, "Failed to save ICICI Breeze credentials"));
    } finally {
      setSaving(false);
    }
  };

  const handleLogin = async () => {
    setSaving(true);
    setMsg("");
    try {
      const r = await getIciciLoginUrl();
      window.open(r.data.login_url, "_blank", "width=700,height=700");
      setMsg("Login page opened. After login, copy the apisession= token from your browser URL bar.");
    } catch (e) {
      setMsg(describeApiError(e, "Save API Key first"));
    } finally {
      setSaving(false);
    }
  };

  const handleConnect = async () => {
    if (!sessionToken) return setMsg("Paste the apisession token");
    setSaving(true);
    setMsg("");
    try {
      await connectIciciBreeze(sessionToken);
      setMsg("✓ Connected!");
      setExpanded(false);
      onRefresh();
    } catch (e) {
      const detail = describeApiError(e, "Connection failed");
      const d = detail.toLowerCase();
      if (
        d.includes("public key") ||
        d.includes("check api key") ||
        d.includes("api key is not recognised") ||
        d.includes("appkey") ||
        d.includes("not recognised by icici") ||
        d.includes("my apps")
      ) {
        setMsg(
          "❌ API Key not registered — your App Key was rejected by ICICI.\n" +
            "Fix: api.icicidirect.com → 'My Apps' → create/activate your app → copy the exact App Key into Step 1, then Save.",
        );
      } else if (d.includes("session key") || d.includes("invalid session") || d.includes("session token is invalid")) {
        setMsg("⏰ Session token invalid — open ICICI Direct Login again and paste the fresh token immediately (~30s window).");
      } else if (d.includes("expired") || d.includes("resource not available")) {
        setMsg("⏰ Token expired — login again, copy apisession, paste & connect immediately.");
      } else {
        setMsg(detail);
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <BrokerCard
      badge="IC"
      badgeTone="border-accent-red/30 bg-accent-red/12 text-accent-red"
      name={
        <>
          ICICI Direct <span className="text-[11px] font-normal text-text-muted">(Breeze)</span>
        </>
      }
      status={status}
      credsChip={<CredsSavedChip fields={savedFields} />}
      onDisconnect={() => disconnectBroker("icici_breeze").then(onRefresh)}
      tags={<StatusBadge label="3yr F&O" variant="success" />}
      expanded={expanded}
      setExpanded={setExpanded}
    >
      <InfoNote tone="amber">
        <div className="font-bold">Breeze API credentials (not your banking login)</div>
        <p className="mt-0.5 text-[11px] text-text-muted">
          Get API Key + Secret from{" "}
          <a href="https://api.icicidirect.com" target="_blank" rel="noopener noreferrer" className="text-accent-blue hover:underline">
            api.icicidirect.com
          </a>{" "}
          → <strong className="text-text-primary">My Apps</strong> → use the app&apos;s <strong className="text-text-primary">App Key</strong> (not client
          ID) and <strong className="text-text-primary">Secret Key</strong>.
        </p>
        <p className="mt-0.5 text-[11px] text-accent-red/80">⚠ &quot;Public key does not exist&quot; = wrong App Key — re-copy from My Apps.</p>
      </InfoNote>

      {hasCreds ? <InfoNote tone="blue" icon={<CheckCircle2 size={12} />}>Credentials saved — skip Step 1 and go to login.</InfoNote> : null}
      {status?.detail && !ready ? <InfoNote tone="amber" icon={<AlertCircle size={12} />}>{status.detail}</InfoNote> : null}

      <div className="space-y-2">
        <StepHeading>Step 1 — API Credentials (one-time)</StepHeading>
        <TextField value={apiKey} onChange={setApiKey} label="API Key (from api.icicidirect.com)" placeholder="J279)$5v…" saved={savedFields["api_key"]} />
        <PasswordField value={secret} onChange={setSecret} label="Secret Key" placeholder="xi3j020b…" saved={savedFields["secret"]} />
        <button type="button" onClick={handleSave} disabled={saving} className={SAVE_BTN}>
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />} Save Credentials
        </button>
      </div>

      <div className="space-y-2">
        <StepHeading>Step 2 — Login to ICICI Direct</StepHeading>
        <InfoNote tone="red" icon={<AlertCircle size={11} />}>
          <strong>Token expires in ~30 seconds!</strong> After the redirect, immediately copy the apisession token and paste it below.
        </InfoNote>
        <button
          type="button"
          onClick={handleLogin}
          disabled={saving}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent-red/35 bg-accent-red/12 py-2 text-[13px] font-semibold text-accent-red hover:bg-accent-red/20 disabled:opacity-50"
        >
          {saving ? <Loader2 size={14} className="animate-spin" /> : <ExternalLink size={14} />} Open ICICI Direct Login
        </button>
      </div>

      <div className="space-y-2">
        <StepHeading>Step 3 — Paste Session Token</StepHeading>
        <p className="text-[11px] text-text-muted">
          From URL: <code className="rounded bg-bg-primary/40 px-1 text-accent-green">…/?apisession=<strong>TOKEN</strong></code>
        </p>
        <PasswordField
          value={sessionToken}
          onChange={(v) => {
            setSessionToken(v);
            setTokenAge(0);
            if (tokenAgeRef.current) clearInterval(tokenAgeRef.current);
            if (v) tokenAgeRef.current = setInterval(() => setTokenAge((a) => a + 1), 1000);
          }}
          label="apisession token (from redirect URL)"
          placeholder="paste the token here"
        />
        {sessionToken && tokenAge > 0 ? (
          <p className={clsx("font-mono text-[11px]", tokenAge >= 25 ? "animate-pulse text-accent-red" : tokenAge >= 15 ? "text-accent-amber" : "text-accent-green")}>
            ⏱ Token age: {tokenAge}s{tokenAge >= 30 ? " — EXPIRED, get a fresh token!" : tokenAge >= 15 ? " — connect now!" : " — good"}
          </p>
        ) : null}
        <button
          type="button"
          onClick={handleConnect}
          disabled={saving || !sessionToken}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-accent-red/35 bg-accent-red/12 py-2 text-[13px] font-semibold text-accent-red hover:bg-accent-red/20 disabled:opacity-50"
        >
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />} Connect ICICI Breeze
        </button>
      </div>
      <Feedback msg={msg} />
    </BrokerCard>
  );
}

// ── Telegram ──────────────────────────────────────────────────────────────────

type TelegramChat = { chat_id: string; title?: string; type?: string; username?: string };

function TelegramCard() {
  const qc = useQueryClient();
  const { data: allCreds } = useAllCredsStatus();
  const savedFields: Record<string, boolean> = {
    bot_token: Boolean(allCreds?.telegram?.fields?.bot_token),
    chat_id: Boolean(allCreds?.telegram?.fields?.chat_id),
  };

  const { data, isLoading } = useQuery({
    queryKey: ["telegramSettings"],
    queryFn: () => getTelegramSettings().then((r) => r.data),
    staleTime: 15_000,
    refetchOnWindowFocus: false,
  });

  const [expanded, setExpanded] = useState(false);
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [interval, setIntervalValue] = useState("1h");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    if (!data) return;
    setEnabled(Boolean(data.enabled));
    setIntervalValue(String(data.report_interval || "1h"));
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
    onError: (e) => setMsg(describeApiError(e, "Failed to save Telegram settings")),
  });

  const discoverMut = useMutation({
    mutationFn: () => discoverTelegramChats(botToken.trim()),
    onError: (e) => setMsg(describeApiError(e, "Failed to discover Telegram chats")),
  });

  const testMut = useMutation({
    mutationFn: () => sendTelegramTest(),
    onSuccess: (res) => setMsg(res.data?.message || "Telegram test message sent."),
    onError: (e) => setMsg(describeApiError(e, "Failed to send Telegram test message")),
  });

  const chats: TelegramChat[] = discoverMut.data?.data?.chats ?? [];
  const active = Boolean(data?.enabled && data?.has_destination);

  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-accent-blue/30 bg-accent-blue/12 text-[11px] font-bold text-accent-blue">
            TG
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-text-primary">Telegram Trade Reports</span>
              {active ? <StatusBadge label="Active" variant="success" icon={<CheckCircle2 size={11} />} /> : null}
              <CredsSavedChip fields={savedFields} />
            </div>
            <div className="mt-0.5 text-[11px] text-text-muted">Send paper-strategy statistics at the selected interval.</div>
          </div>
        </div>
        <button type="button" onClick={() => setExpanded(!expanded)} className="rounded p-1 text-text-muted hover:text-text-primary">
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </button>
      </div>

      {expanded ? (
        <div className="mt-3 space-y-3 border-t border-bg-border/70 pt-3">
          <div className="grid gap-3 md:grid-cols-2">
            <PasswordField value={botToken} onChange={setBotToken} label="Bot Token" placeholder="123456:ABC…" saved={savedFields["bot_token"]} />
            <TextField value={chatId} onChange={setChatId} label="Chat ID" placeholder="-1001234567890" saved={savedFields["chat_id"]} />
          </div>

          <div className="space-y-2 rounded-lg border border-bg-border bg-bg-primary/20 p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-text-secondary">Auto-detect Chat ID</div>
                <div className="text-[11px] text-text-muted">Reads recent Telegram updates and lists chats the bot has seen.</div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setMsg("");
                  discoverMut.mutate();
                }}
                disabled={discoverMut.isPending || (!botToken.trim() && !savedFields["bot_token"])}
                className={SAVE_BTN}
              >
                {discoverMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <RefreshCw size={11} />} Detect Chats
              </button>
            </div>
            <p className="text-[10.5px] text-text-muted">
              If nothing is found, send <code className="rounded bg-bg-primary/40 px-1 text-accent-green">/start</code> to the bot or add it to the
              target group, then detect again.
            </p>
            {chats.length ? (
              <div className="space-y-1.5">
                {chats.map((chat) => (
                  <div key={chat.chat_id} className="flex items-center justify-between gap-3 rounded-lg border border-bg-border bg-bg-primary/30 px-3 py-2 text-[11.5px]">
                    <div className="min-w-0">
                      <div className="truncate font-semibold text-text-primary">{chat.title || chat.chat_id}</div>
                      <div className="text-text-muted">
                        {chat.type}
                        {chat.username ? ` · @${chat.username}` : ""} · {chat.chat_id}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => setChatId(chat.chat_id)}
                      className="shrink-0 rounded-lg border border-accent-blue/30 bg-accent-blue/10 px-2 py-1 text-accent-blue hover:bg-accent-blue/20"
                    >
                      Use
                    </button>
                  </div>
                ))}
              </div>
            ) : discoverMut.isSuccess ? (
              <p className="text-[11px] text-text-muted">{discoverMut.data?.data?.hint || "No chats were discovered yet."}</p>
            ) : null}
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <FieldLabel>Report Interval</FieldLabel>
              <select value={interval} onChange={(e) => setIntervalValue(e.target.value)} className="terminal-input w-full text-sm">
                <option value="30m">Every 30 minutes</option>
                <option value="1h">Hourly</option>
                <option value="4h">Every 4 hours</option>
                <option value="daily">Daily</option>
              </select>
            </div>
            <label className="flex items-center gap-2 self-end rounded-lg border border-bg-border bg-bg-primary/20 px-3 py-2 text-[12.5px] text-text-secondary">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="rounded" />
              Enable Telegram reports
            </label>
          </div>

          <p className="text-[11px] text-text-muted">
            {isLoading
              ? "Loading current Telegram settings…"
              : data?.has_destination
                ? `Current interval: ${data.report_interval}. Reports are ${data.enabled ? "enabled" : "disabled"}.`
                : "Save a bot token and chat ID to enable Telegram delivery."}
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setMsg("");
                saveMut.mutate({ bot_token: botToken, chat_id: chatId, enabled, report_interval: interval });
              }}
              disabled={saveMut.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent-blue/35 bg-accent-blue/12 px-3 py-1.5 text-[12px] font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50"
            >
              {saveMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Telegram Settings
            </button>
            <button
              type="button"
              onClick={() => {
                setMsg("");
                testMut.mutate();
              }}
              disabled={testMut.isPending || !data?.has_destination}
              className="inline-flex items-center gap-1.5 rounded-lg border border-accent-green/35 bg-accent-green/12 px-3 py-1.5 text-[12px] font-semibold text-accent-green hover:bg-accent-green/20 disabled:opacity-50"
            >
              {testMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />} Send Test Message
            </button>
          </div>
          <Feedback msg={msg} />
        </div>
      ) : null}
    </div>
  );
}

// ── Trading calendar ──────────────────────────────────────────────────────────

type CalendarException = { date: string; name?: string; status: "closed" | "partial" | "open"; sessions?: string[] };
type CalendarExchangeConfig = {
  enabled: boolean;
  sessions: Array<{ key: string; label?: string; open: string; close: string }>;
  exceptions: CalendarException[];
};

function formatCalendarLines(exceptions: CalendarException[] = []): string {
  return exceptions
    .map((item) => {
      const sessions = (item.sessions || []).join(",");
      const status = item.status === "partial" && sessions ? `partial:${sessions}` : item.status;
      return [item.date, status, item.name || ""].join(" | ").trim();
    })
    .join("\n");
}

function parseCalendarLines(value: string): CalendarException[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split("|").map((p) => p.trim());
      const date = parts[0] || "";
      const [rawStatus, rawSessions] = (parts[1] || "closed").toLowerCase().split(":", 2);
      const status: CalendarException["status"] = rawStatus === "partial" ? "partial" : rawStatus === "open" ? "open" : "closed";
      const sessions = rawSessions ? rawSessions.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean) : [];
      return { date, status, sessions, name: parts.slice(2).join(" | ") } as CalendarException;
    })
    .filter((item) => /^\d{4}-\d{2}-\d{2}$/.test(item.date));
}

function CalendarStatusTile({ label, status }: { label: string; status?: any }) {
  const open = Boolean(status?.is_open);
  const sessionLabel = status?.active_session?.label || status?.active_session?.key;
  return (
    <div className="rounded-xl border border-bg-border bg-bg-primary/15 p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-text-muted">{label}</div>
        <span className={clsx("inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em]", serviceStateTone(open ? "active" : status?.reason === "outside_session" ? null : "stale"))}>
          {open ? "Open" : "Closed"}
        </span>
      </div>
      <div className="mt-2 font-mono text-sm text-text-primary">{open ? sessionLabel || "Trading" : humanize(status?.reason) || "closed"}</div>
      <div className="mt-1 text-[11px] text-text-muted">Next: {status?.next_open_at ? formatIST(status.next_open_at) : "—"}</div>
    </div>
  );
}

function TradingCalendarCard() {
  const qc = useQueryClient();
  const [enabled, setEnabled] = useState(true);
  const [nseLines, setNseLines] = useState("");
  const [mcxLines, setMcxLines] = useState("");
  const [msg, setMsg] = useState("");

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["tradingCalendar"],
    queryFn: () => getTradingCalendar().then((r) => r.data),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    const config = data?.config;
    if (!config) return;
    setEnabled(Boolean(config.enabled));
    setNseLines(formatCalendarLines(config.exchanges?.NSE?.exceptions || []));
    setMcxLines(formatCalendarLines(config.exchanges?.MCX?.exceptions || []));
  }, [data]);

  const saveMut = useMutation({
    mutationFn: async () => {
      const config = data?.config || {};
      const nse: CalendarExchangeConfig =
        config.exchanges?.NSE || { enabled: true, sessions: [{ key: "regular", label: "Regular", open: "09:15", close: "15:30" }], exceptions: [] };
      const mcx: CalendarExchangeConfig =
        config.exchanges?.MCX || {
          enabled: true,
          sessions: [
            { key: "morning", label: "Morning", open: "09:00", close: "17:00" },
            { key: "evening", label: "Evening", open: "17:00", close: "23:30" },
          ],
          exceptions: [],
        };
      return updateTradingCalendar({
        enabled,
        exchanges: {
          NSE: { ...nse, exceptions: parseCalendarLines(nseLines) },
          MCX: { ...mcx, exceptions: parseCalendarLines(mcxLines) },
        },
      }).then((r) => r.data);
    },
    onSuccess: (payload) => {
      qc.setQueryData(["tradingCalendar"], payload);
      qc.invalidateQueries({ queryKey: ["riskStatus"] });
      qc.invalidateQueries({ queryKey: ["brokerStatus"] });
      setMsg("✓ Trading calendar saved");
      setTimeout(() => setMsg(""), 2500);
    },
    onError: (e) => setMsg(describeApiError(e, "Failed to save trading calendar")),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <CalendarDays size={16} className="text-accent-blue" />
          <div>
            <div className="text-sm font-semibold text-text-primary">Trading Calendar Gate</div>
            <div className="text-[11px] text-text-muted">Paper entries follow NSE and MCX exchange sessions.</div>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setEnabled((v) => !v)}
          className={clsx(
            "inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11.5px] font-semibold",
            enabled ? "border-accent-green/30 bg-accent-green/10 text-accent-green" : "border-bg-border bg-bg-secondary/40 text-text-muted",
          )}
        >
          <ShieldCheck size={12} /> {enabled ? "Gate On" : "Gate Off"}
        </button>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <CalendarStatusTile label="NSE" status={data?.status?.NSE} />
        <CalendarStatusTile label="MCX" status={data?.status?.MCX} />
      </div>

      {isError ? <InfoNote tone="amber" icon={<AlertCircle size={12} />}>{describeApiError(error, "Trading calendar settings are unavailable.")}</InfoNote> : null}

      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <FieldLabel>NSE exceptions</FieldLabel>
          <textarea value={nseLines} onChange={(e) => setNseLines(e.target.value)} className="terminal-input min-h-[170px] w-full resize-y text-[11.5px]" spellCheck={false} />
        </div>
        <div>
          <FieldLabel>MCX exceptions</FieldLabel>
          <textarea value={mcxLines} onChange={(e) => setMcxLines(e.target.value)} className="terminal-input min-h-[170px] w-full resize-y text-[11.5px]" spellCheck={false} />
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-[11px] text-text-muted">
          Format: <code className="rounded bg-bg-primary/40 px-1 text-accent-green">YYYY-MM-DD | closed</code> or{" "}
          <code className="rounded bg-bg-primary/40 px-1 text-accent-green">YYYY-MM-DD | partial:evening</code>
        </div>
        <button
          type="button"
          onClick={() => saveMut.mutate()}
          disabled={saveMut.isPending || isLoading || !data}
          className="inline-flex items-center gap-1.5 rounded-lg border border-accent-blue/35 bg-accent-blue/12 px-3 py-1.5 text-[12px] font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50"
        >
          {saveMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Save Calendar
        </button>
      </div>
      <Feedback msg={msg} />
    </div>
  );
}

// ── Risk controls ─────────────────────────────────────────────────────────────

type RiskConfig = {
  max_loss_per_trade?: number;
  max_daily_loss?: number;
  max_open_positions?: number;
  concentration_limit?: number;
  max_sector_positions?: number;
};
type RiskStatus = {
  trading_allowed?: boolean;
  daily_loss?: number;
  max_daily_loss?: number;
  open_positions?: number;
  max_positions?: number;
  sizing_mode?: string;
  config?: RiskConfig;
};

const RISK_FIELDS: Array<{ key: keyof RiskConfig; label: string; hint: string; placeholder: string; integer?: boolean }> = [
  { key: "max_loss_per_trade", label: "Max Loss / Trade ₹", hint: "per-position stop budget", placeholder: "5000" },
  { key: "max_daily_loss", label: "Daily Loss Limit ₹", hint: "halts new entries", placeholder: "15000" },
  { key: "max_open_positions", label: "Max Open Positions", hint: "concurrent positions", placeholder: "5", integer: true },
  { key: "concentration_limit", label: "Concentration Limit", hint: "fraction of deployed capital (0–1)", placeholder: "0.40" },
  { key: "max_sector_positions", label: "Max Sector Positions", hint: "per-sector cap", placeholder: "3", integer: true },
];

function RiskControlsCard() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["riskStatus"],
    queryFn: () => getRiskStatus().then((r) => r.data as RiskStatus),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");

  const cfg = data?.config;

  const valueFor = (key: keyof RiskConfig): string => {
    if (draft[key] !== undefined) return draft[key];
    const v = cfg?.[key];
    return v == null ? "" : String(v);
  };

  const riskMut = useMutation({
    mutationFn: (payload: RiskConfig) => updateRiskConfig(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["riskStatus"] });
      setDraft({});
      setMsg("✓ Risk config updated");
      setTimeout(() => setMsg(""), 2500);
    },
    onError: (e) => setMsg(describeApiError(e, "Failed to update risk config")),
  });

  const handleSave = () => {
    // Only send fields that have a usable numeric value (edited or pre-filled);
    // never clobber an existing config value with NaN from an empty edit.
    const payload: RiskConfig = {};
    for (const f of RISK_FIELDS) {
      const raw = valueFor(f.key).trim();
      if (raw === "") continue;
      const num = Number(raw);
      if (Number.isNaN(num)) continue;
      payload[f.key] = f.integer ? Math.round(num) : num;
    }
    if (Object.keys(payload).length === 0) {
      setMsg("Nothing to save — enter at least one value.");
      return;
    }
    setMsg("");
    riskMut.mutate(payload);
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MetricTile label="Trading" value={data?.trading_allowed ? "Allowed" : "Halted"} color={data?.trading_allowed ? "text-accent-green" : "text-accent-red"} size="sm" />
        <MetricTile label="Daily loss" value={formatMoney(data?.daily_loss, 0)} detail={`of ${formatMoney(data?.max_daily_loss, 0)}`} color={tone(-(data?.daily_loss ?? 0))} size="sm" />
        <MetricTile label="Open positions" value={`${data?.open_positions ?? 0} / ${data?.max_positions ?? 0}`} size="sm" />
        <MetricTile label="Sizing mode" value={humanize(data?.sizing_mode) || "—"} color={data?.sizing_mode === "normal" ? undefined : "text-accent-amber"} size="sm" />
      </div>

      <div className="rounded-xl border border-bg-border bg-bg-primary/14 p-3.5">
        {isLoading && !data ? (
          <div className="py-6 text-center text-sm text-text-muted">Loading risk config…</div>
        ) : (
          <div className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {RISK_FIELDS.map((f) => (
                <div key={f.key}>
                  <FieldLabel>{f.label}</FieldLabel>
                  <input
                    value={valueFor(f.key)}
                    onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                    placeholder={f.placeholder}
                    inputMode="decimal"
                    className="terminal-input w-full text-sm"
                  />
                  <div className="mt-0.5 text-[10px] text-text-muted">{f.hint}</div>
                </div>
              ))}
            </div>
            <div className="flex items-center justify-between gap-3">
              <Feedback msg={msg} />
              <button
                type="button"
                onClick={handleSave}
                disabled={riskMut.isPending}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-accent-blue/35 bg-accent-blue/12 px-3 py-1.5 text-[12px] font-semibold text-accent-blue hover:bg-accent-blue/20 disabled:opacity-50"
              >
                {riskMut.isPending ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />} Update Risk Config
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

export default function SettingsPanel() {
  const qc = useQueryClient();
  const layoutBrokerStatuses = useStore((s) => s.brokerStatuses) as unknown as BrokerStatusEntry[];

  const {
    data: brokerStatuses,
    isError: brokerStatusError,
    isFetching,
    dataUpdatedAt,
    refetch: refetchBrokers,
  } = useQuery({
    queryKey: ["brokerStatus"],
    queryFn: () => getBrokerStatus().then((r) => r.data as BrokerStatusEntry[]),
    refetchInterval: REFRESH_MS.snapshot * 4,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const effective = useMemo(() => mergeBrokerStatuses(brokerStatuses, layoutBrokerStatuses), [brokerStatuses, layoutBrokerStatuses]);
  const statusMap = useMemo(() => Object.fromEntries(effective.map((s) => [s.broker, s])), [effective]);
  const showingFallback = Boolean((brokerStatusError || !brokerStatuses?.length) && layoutBrokerStatuses.length);

  const readyCount = effective.filter((s) => isBrokerReady(s)).length;
  const reconnectCount = effective.filter((s) => !isBrokerReady(s) && s.needs_reconnect).length;

  const handleRefresh = useCallback(async () => {
    try {
      const r = await getBrokerStatus({ forceValidate: true });
      qc.setQueryData(["brokerStatus"], r.data);
    } catch {
      await refetchBrokers();
    }
    qc.invalidateQueries({ queryKey: ["allCredsStatus"] });
    qc.invalidateQueries({ queryKey: ["riskStatus"] });
  }, [qc, refetchBrokers]);

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <header className="rounded-2xl border border-bg-border bg-bg-secondary/22 px-5 py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-text-primary">Settings</h1>
            <p className="mt-1 text-sm text-text-muted">
              Broker connections, Telegram delivery, the trading-calendar gate, and portfolio risk limits.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <a
              href={V1_HREF}
              className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
              title="Open the v1 settings page"
            >
              <Link2 size={13} /> v1
            </a>
            <button
              type="button"
              onClick={handleRefresh}
              className="inline-flex items-center gap-1.5 rounded-lg border border-bg-border bg-bg-primary/30 px-2.5 py-1.5 text-[11.5px] text-text-secondary hover:border-bg-active hover:text-text-primary"
            >
              <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} /> Refresh
            </button>
          </div>
        </div>
      </header>

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricTile label="Brokers ready" value={`${readyCount} / ${effective.length || 4}`} detail="active sessions" color={readyCount ? "text-accent-green" : undefined} />
        <MetricTile label="Need reconnect" value={String(reconnectCount)} detail="expired sessions" color={reconnectCount ? "text-accent-amber" : undefined} />
        <MetricTile label="Status feed" value={brokerStatusError ? "offline" : "live"} detail={dataUpdatedAt ? formatIST(dataUpdatedAt) : ""} color={brokerStatusError ? "text-accent-red" : "text-accent-green"} />
        <MetricTile label="Source" value={showingFallback ? "layout bar" : "broker API"} detail={showingFallback ? "fallback" : "validated"} color={showingFallback ? "text-accent-amber" : undefined} />
      </section>

      <Section title="Broker connections" icon={<Plug size={16} />} description="API credentials are saved once; same-day sessions auto-restore across refreshes until the broker expires them.">
        {showingFallback ? (
          <div className="mb-3">
            <InfoNote tone="amber" icon={<AlertCircle size={12} />}>
              Live broker validation is temporarily unavailable. Showing the last state received by the layout bar.
            </InfoNote>
          </div>
        ) : null}
        <div className="space-y-3">
          <FyersCard status={statusMap["fyers"]} onRefresh={handleRefresh} />
          <UpstoxCard status={statusMap["upstox"]} onRefresh={handleRefresh} />
          <FivePaisaCard status={statusMap["fivepaisa"]} onRefresh={handleRefresh} />
          <ICICIBreezeCard status={statusMap["icici_breeze"]} onRefresh={handleRefresh} />
        </div>
      </Section>

      <Section title="Notifications" icon={<Send size={16} />} description="Telegram delivery for paper-strategy trade reports.">
        <TelegramCard />
      </Section>

      <Section title="Trading calendar" icon={<CalendarDays size={16} />}>
        <TradingCalendarCard />
      </Section>

      <Section title="Risk controls" icon={<ShieldCheck size={16} />} description="Portfolio-level loss caps and position limits enforced by the live risk manager.">
        <RiskControlsCard />
      </Section>
    </div>
  );
}

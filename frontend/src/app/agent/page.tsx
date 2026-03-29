"use client";
import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { chatWithAgent, getAgentLog, getRulesStatus, runScan } from "@/lib/api";
import { clsx } from "clsx";
import { Send, Bot, Zap, Play } from "lucide-react";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

export default function AgentPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: "assistant",
      content: "Hello! I'm CURIE, your NSE F&O trading assistant. I can analyze option chains, market profiles, and generate trade proposals. How can I help you today?",
      timestamp: new Date().toISOString(),
    },
  ]);
  const [input, setInput] = useState("");
  const chatBottomRef = useRef<HTMLDivElement>(null);

  const { data: agentLog } = useQuery({
    queryKey: ["agentLog"],
    queryFn: () => getAgentLog(20).then((r) => r.data),
    refetchInterval: 10000,
  });

  const { data: rules } = useQuery({
    queryKey: ["rulesStatus"],
    queryFn: () => getRulesStatus().then((r) => r.data),
    refetchInterval: 30000,
  });

  const chatMut = useMutation({
    mutationFn: chatWithAgent,
    onSuccess: (res) => {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: res.data.response, timestamp: new Date().toISOString() },
      ]);
    },
  });

  const scanMut = useMutation({
    mutationFn: () => runScan(),
  });

  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = () => {
    if (!input.trim()) return;
    const msg = input.trim();
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content: msg, timestamp: new Date().toISOString() },
    ]);
    chatMut.mutate(msg);
  };

  return (
    <div className="max-w-screen-xl space-y-4 h-[calc(100vh-12rem)]">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold font-mono text-text-primary flex items-center gap-2">
          <Bot size={18} className="text-accent-purple" />
          CURIE Agent Console
        </h1>
        <button
          onClick={() => scanMut.mutate()}
          disabled={scanMut.isPending}
          className="px-3 py-1.5 rounded text-xs bg-accent-purple/20 border border-accent-purple/30 text-accent-purple hover:bg-accent-purple/30 flex items-center gap-2 disabled:opacity-50"
        >
          <Play size={12} />
          {scanMut.isPending ? "Scanning..." : "Run Scan"}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 h-full">
        {/* Chat */}
        <div className="lg:col-span-2 card p-4 flex flex-col h-[calc(100vh-16rem)]">
          <div className="flex-1 overflow-y-auto space-y-3 mb-3">
            {messages.map((msg, i) => (
              <div
                key={i}
                className={clsx("flex gap-2", msg.role === "user" ? "justify-end" : "justify-start")}
              >
                {msg.role === "assistant" && (
                  <div className="w-6 h-6 rounded-full bg-accent-purple/20 flex items-center justify-center shrink-0 mt-1">
                    <Bot size={12} className="text-accent-purple" />
                  </div>
                )}
                <div
                  className={clsx(
                    "max-w-[80%] rounded-lg px-3 py-2 text-sm",
                    msg.role === "user"
                      ? "bg-accent-blue/20 text-text-primary"
                      : "bg-bg-secondary text-text-secondary"
                  )}
                >
                  <p className="whitespace-pre-wrap">{msg.content}</p>
                  <span className="text-text-muted text-xs mt-1 block">
                    {msg.timestamp.slice(11, 16)}
                  </span>
                </div>
              </div>
            ))}
            {chatMut.isPending && (
              <div className="flex gap-2">
                <div className="w-6 h-6 rounded-full bg-accent-purple/20 flex items-center justify-center">
                  <Bot size={12} className="text-accent-purple" />
                </div>
                <div className="bg-bg-secondary rounded-lg px-3 py-2 text-sm text-text-muted">
                  <span className="animate-pulse">Analyzing...</span>
                </div>
              </div>
            )}
            <div ref={chatBottomRef} />
          </div>
          <div className="flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && handleSend()}
              placeholder="Ask CURIE about market conditions, option strategies..."
              className="terminal-input flex-1 text-sm"
            />
            <button
              onClick={handleSend}
              disabled={chatMut.isPending || !input.trim()}
              className="px-3 py-2 rounded bg-accent-purple/20 border border-accent-purple/30 text-accent-purple hover:bg-accent-purple/30 disabled:opacity-50"
            >
              <Send size={14} />
            </button>
          </div>
        </div>

        {/* Rules + Log */}
        <div className="space-y-4">
          {/* Rules Engine Status */}
          <div className="card p-4">
            <h2 className="text-sm text-text-secondary mb-3 flex items-center gap-2">
              <Zap size={14} /> Tier 1 Rules
            </h2>
            <div className="space-y-2">
              {rules?.enabled_rules && Object.entries(rules.enabled_rules).map(([rule, enabled]: [string, any]) => (
                <div key={rule} className="flex items-center justify-between text-xs">
                  <span className="text-text-muted font-mono">{rule.replace(/_/g, " ")}</span>
                  <span className={clsx(
                    "px-2 py-0.5 rounded font-bold",
                    enabled ? "bg-accent-green/20 text-accent-green" : "bg-bg-tertiary text-text-muted"
                  )}>
                    {enabled ? "ON" : "OFF"}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Agent Run Log */}
          <div className="card p-4">
            <h2 className="text-sm text-text-secondary mb-3">Agent Log</h2>
            <div className="space-y-2 overflow-y-auto max-h-48">
              {agentLog && agentLog.length > 0 ? (
                agentLog.slice(0, 10).map((log: any, i: number) => (
                  <div key={i} className="text-xs bg-bg-secondary rounded p-2 space-y-1">
                    <div className="flex justify-between">
                      <span className="text-accent-purple font-mono">Tier {log.tier}</span>
                      <span className="text-text-muted">{log.timestamp?.slice(11, 19)}</span>
                    </div>
                    <div className="text-text-muted line-clamp-2">{log.symbol}</div>
                    {log.proposal && (
                      <div className={clsx(
                        "px-2 py-0.5 rounded inline-block text-xs font-bold",
                        log.proposal.confidence === "HIGH" ? "bg-accent-green/20 text-accent-green" : "bg-accent-amber/20 text-accent-amber"
                      )}>
                        {log.proposal.confidence} — {log.proposal.strategy}
                      </div>
                    )}
                  </div>
                ))
              ) : (
                <div className="text-text-muted text-xs text-center py-4">
                  No agent activity yet.<br/>Run a scan to start.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

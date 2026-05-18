"use client";

import dynamic from "next/dynamic";
import Image from "next/image";
import { useCallback, useEffect, useState } from "react";
import { v4 as uuidv4 } from "uuid";
import IngestPanel from "@/app/components/IngestPanel";
import StreamingAnswer, { NodeEvent } from "@/app/components/StreamingAnswer";
import ThemeToggle from "@/app/components/ThemeToggle";

const GraphVisualization = dynamic(
  () => import("@/app/components/GraphVisualization"),
  { ssr: false }
);

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface HealthState {
  status: "ok" | "down" | "checking";
  neo4j: string;
  qdrant: string;
}

const SUGGESTIONS = [
  "What does this codebase do?",
  "List all classes and their methods",
  "Find authentication-related logic",
  "Explain the main entry points",
];

export default function Home() {
  const [query, setQuery] = useState("");
  const [threadId, setThreadId] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [events, setEvents] = useState<NodeEvent[]>([]);
  const [finalAnswer, setFinalAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [graphRefreshKey, setGraphRefreshKey] = useState(0);
  const [ingestUiReset, setIngestUiReset] = useState(0);
  const [ingestedThisSession, setIngestedThisSession] = useState(false);

  const handleIngestCleared = useCallback(() => {
    setGraphRefreshKey((k) => k + 1);
    setIngestUiReset((k) => k + 1);
    setIngestedThisSession(false);
  }, []);

  const handleIngestComplete = useCallback(() => {
    setGraphRefreshKey((k) => k + 1);
    setIngestedThisSession(true);
  }, []);
  const [threadStatus, setThreadStatus] = useState<{
    loading: boolean;
    found: boolean;
    humanTurns: number;
    error: string | null;
  }>({ loading: false, found: false, humanTurns: 0, error: null });
  const [loadedFinalAnswer, setLoadedFinalAnswer] = useState("");
  const [health, setHealth] = useState<HealthState>({
    status: "checking",
    neo4j: "?",
    qdrant: "?",
  });

  // Health polling
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const res = await fetch(`${API}/health`);
        if (!res.ok) throw new Error("down");
        const j = await res.json();
        if (cancelled) return;
        setHealth({
          status: "ok",
          neo4j: j.neo4j ?? "?",
          qdrant: j.qdrant ?? "?",
        });
      } catch {
        if (cancelled) return;
        setHealth({ status: "down", neo4j: "?", qdrant: "?" });
      }
    };
    check();
    const t = setInterval(check, 15000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  const loadThread = useCallback(async (id?: string) => {
    const tid = (id ?? threadId).trim();
    if (!tid) {
      setThreadStatus({ loading: false, found: false, humanTurns: 0, error: null });
      setLoadedFinalAnswer("");
      return;
    }

    setThreadStatus((s) => ({ ...s, loading: true, error: null }));
    try {
      const res = await fetch(`${API}/history/${encodeURIComponent(tid)}`);
      const data = await res.json();
      if (!res.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
      }
      setThreadId(tid);
      setThreadStatus({
        loading: false,
        found: Boolean(data.found),
        humanTurns: data.human_turns ?? 0,
        error: null,
      });
      setLoadedFinalAnswer(data.final_answer ?? "");
      if (!data.found) {
        setThreadStatus((s) => ({
          ...s,
          error: "No saved conversation for this ID on this server.",
        }));
      }
    } catch (err) {
      setThreadStatus({
        loading: false,
        found: false,
        humanTurns: 0,
        error: err instanceof Error ? err.message : String(err),
      });
      setLoadedFinalAnswer("");
    }
  }, [threadId]);

  const handleAnalyze = useCallback(async (q?: string) => {
    const finalQuery = (q ?? query).trim();
    if (!finalQuery || isStreaming) return;
    if (q !== undefined) setQuery(q);

    const tid = threadId.trim() || uuidv4();
    setThreadId(tid);
    setEvents([]);
    setFinalAnswer(loadedFinalAnswer);
    setError(null);
    setIsStreaming(true);

    try {
      const res = await fetch(`${API}/analyze/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: finalQuery, thread_id: tid }),
      });

      if (!res.ok || !res.body) {
        const text = await res.text().catch(() => `HTTP ${res.status}`);
        throw new Error(text);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() ?? "";
        for (const chunk of lines) {
          const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          const raw = dataLine.slice(6).trim();
          if (raw === "[DONE]") continue;
          try {
            const payload = JSON.parse(raw) as NodeEvent & {
              error?: string;
              state?: { final_answer?: string };
            };
            if (payload.error) { setError(payload.error); continue; }
            setEvents((prev) => [...prev, payload]);
            if (payload.state?.final_answer) {
              setFinalAnswer(payload.state.final_answer as string);
            }
          } catch { /* skip */ }
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsStreaming(false);
    }
  }, [query, threadId, isStreaming, loadedFinalAnswer]);

  const hasResults =
    events.length > 0 || isStreaming || Boolean(finalAnswer) || Boolean(loadedFinalAnswer);

  return (
    <div className="relative min-h-screen flex flex-col">
      {/* ── Top bar ──────────────────────────────────────────────────────── */}
      <header className="relative z-10 flex items-center justify-between gap-4 px-8 h-20 border-b border-[var(--border)] bg-[var(--bg)]/80 backdrop-blur-xl shadow-sm">
        {/* Subtle animated top gradient line */}
        <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-blue-500/50 to-transparent opacity-50"></div>

        <div className="flex items-center gap-4 min-w-0 group cursor-default">
          <div className="relative flex-shrink-0">
            {/* Pulsing glow behind the logo */}
            <div className="absolute -inset-1.5 bg-gradient-to-tr from-blue-500 to-indigo-500 rounded-2xl blur-md opacity-20 group-hover:opacity-40 transition-opacity duration-700"></div>
            <Image
              src="/logo.png"
              alt="CodeGraph AI"
              width={44}
              height={44}
              className="relative h-11 w-11 rounded-[12px] object-cover ring-1 ring-[var(--border)] shadow-md transition-transform duration-500 group-hover:scale-[1.02]"
              priority
            />
          </div>
          <div className="min-w-0 flex flex-col justify-center">
            <h1 className="text-[1.35rem] font-bold tracking-tight bg-gradient-to-br from-[var(--fg-1)] to-[var(--fg-3)] bg-clip-text text-transparent leading-none drop-shadow-sm pb-1">
              CodeGraph AI
            </h1>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-bold tracking-wider uppercase bg-blue-500/10 text-blue-500 ring-1 ring-inset ring-blue-500/20">
                Agentic
              </span>
              <p className="text-[12px] font-medium text-[var(--fg-3)] leading-none tracking-wide">
                Codebase Analyzer
              </p>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-4 flex-shrink-0">
          <ThemeToggle />
          <div 
            className="flex items-center gap-2.5 px-3.5 py-1.5 rounded-full border border-[var(--border)] bg-[var(--surface-1)] shadow-sm transition-colors hover:bg-[var(--surface-2)] cursor-help" 
            title={`Neo4j: ${health.neo4j} · Qdrant: ${health.qdrant}`}
          >
            <div className="relative flex h-2.5 w-2.5">
              {health.status === "ok" && (
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60"></span>
              )}
              <span className={`relative inline-flex rounded-full h-2.5 w-2.5 ${
                health.status === "ok" ? "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]" :
                health.status === "down" ? "bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.6)]" : 
                "bg-amber-500 animate-pulse"
              }`}></span>
            </div>
            <span className="text-[11px] font-semibold text-[var(--fg-2)] tracking-wide">
              {health.status === "ok" ? "Systems Active" :
               health.status === "down" ? "Offline" : "Checking..."}
            </span>
          </div>
        </div>
      </header>

      {/* ── Body ────────────────────────────────────────────────────────── */}
      <div className="relative z-10 flex flex-1 min-h-0">
        {/* Sidebar */}
        <aside className="w-80 flex-shrink-0 border-r border-[var(--border)] bg-[var(--surface-1)] flex flex-col overflow-y-auto">
          <div className="p-5 space-y-5">
            <IngestPanel
              onDataChange={handleIngestComplete}
              onCleared={handleIngestCleared}
              resetKey={ingestUiReset}
            />

            {/* Thread / session */}
            <div className="space-y-2">
              <label className="section-label block">Conversation</label>
              <input
                type="text"
                value={threadId}
                onChange={(e) => {
                  setThreadId(e.target.value);
                  setThreadStatus({ loading: false, found: false, humanTurns: 0, error: null });
                  setLoadedFinalAnswer("");
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void loadThread();
                  }
                }}
                onBlur={() => {
                  if (threadId.trim()) void loadThread();
                }}
                placeholder="Paste thread ID, then press Enter"
                className="input font-mono text-xs"
              />
              <p className="text-[11px] text-[var(--fg-4)] leading-relaxed">
                Paste a thread ID and press <kbd className="px-1 py-0.5 rounded bg-[var(--surface-3)] border border-[var(--border)] font-mono text-[10px]">Enter</kbd> to load it.
                Then ask a follow-up in the query box and click Analyze (or ⌘↵).
              </p>
              {threadStatus.loading && (
                <p className="text-[11px] text-[var(--fg-3)]">Loading thread…</p>
              )}
              {!threadStatus.loading && threadStatus.found && (
                <p className="text-[11px] text-success">
                  Thread loaded · {threadStatus.humanTurns} question{threadStatus.humanTurns === 1 ? "" : "s"} in history
                </p>
              )}
              {threadStatus.error && (
                <p className="text-[11px] text-warning">{threadStatus.error}</p>
              )}
            </div>
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {/* Query area */}
          <div className="px-6 pt-6 pb-4 border-b border-[var(--border)]">
            <div className="max-w-4xl mx-auto w-full space-y-3">
              <div className="card p-1 focus-within:border-[var(--accent)] focus-within:shadow-[0_0_0_3px_var(--accent-soft)] transition-shadow">
                <textarea
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleAnalyze();
                  }}
                  placeholder="Ask anything about your codebase…"
                  rows={2}
                  className="w-full px-3 py-2.5 text-sm text-[var(--fg-1)] placeholder:text-[var(--fg-4)] bg-transparent resize-none focus:outline-none leading-relaxed"
                />
                <div className="flex items-center justify-between gap-3 px-3 pb-2">
                  <span className="text-[11px] text-[var(--fg-4)]">
                    Press <kbd className="px-1.5 py-0.5 rounded bg-[var(--surface-3)] border border-[var(--border)] font-mono text-[10px]">⌘</kbd> +
                    <kbd className="px-1.5 py-0.5 rounded bg-[var(--surface-3)] border border-[var(--border)] font-mono text-[10px] ml-1">↵</kbd> to submit
                  </span>
                  <button
                    onClick={() => handleAnalyze()}
                    disabled={!query.trim() || isStreaming}
                    className="btn-primary"
                  >
                    {isStreaming ? (
                      <>
                        <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none">
                          <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25" />
                          <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
                        </svg>
                        Analyzing
                      </>
                    ) : (
                      <>
                        Analyze
                        <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
                          <path d="M3 8h10M9 4l4 4-4 4" strokeLinecap="round" strokeLinejoin="round"/>
                        </svg>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Suggestions (only when no results yet) */}
              {!hasResults && (
                <div className="flex flex-wrap gap-2 pt-1">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      onClick={() => handleAnalyze(s)}
                      className="chip hover:border-[var(--border-strong)] hover:text-[var(--fg-1)] cursor-pointer transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              )}

              {error && (
                <div className="flex items-start gap-2 rounded-[var(--radius)] border border-red-500/20 bg-red-500/5 px-3 py-2.5">
                  <svg className="w-4 h-4 text-danger flex-shrink-0 mt-0.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <circle cx="8" cy="8" r="6.5"/>
                    <path d="M8 5v3.5M8 11v.5" strokeLinecap="round"/>
                  </svg>
                  <p className="text-xs text-danger leading-relaxed">{error}</p>
                </div>
              )}
            </div>
          </div>

          {/* Results */}
          <div className="flex-1 overflow-y-auto">
            <div className="max-w-4xl mx-auto w-full px-6 py-6 space-y-6">
              <StreamingAnswer
                events={events}
                isStreaming={isStreaming}
                finalAnswer={finalAnswer || loadedFinalAnswer}
              />

              {/* Knowledge Graph section */}
              <section className="space-y-3 pt-2">
                <div className="flex items-center gap-2">
                  <h2 className="section-label">Knowledge Graph</h2>
                  <span className="h-px flex-1 bg-[var(--border)]" />
                </div>
                <GraphVisualization
                  refreshKey={graphRefreshKey}
                  onCleared={handleIngestCleared}
                  ingestedThisSession={ingestedThisSession}
                />
              </section>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

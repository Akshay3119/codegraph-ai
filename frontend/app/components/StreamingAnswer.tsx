"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";

export interface NodeEvent {
  node: string;
  label: string;
  state: Record<string, unknown>;
}

interface StreamingAnswerProps {
  events: NodeEvent[];
  isStreaming: boolean;
  finalAnswer: string;
}

const NODE_META: Record<string, { dot: string; title: string; tint: string }> = {
  query_analyzer:    { dot: "#60a5fa", title: "Query Analyzer",        tint: "rgba(96, 165, 250, 0.08)" },
  cypher_generation: { dot: "#fbbf24", title: "Graph Query (Cypher)",  tint: "rgba(251, 191, 36, 0.08)" },
  vector_search:     { dot: "#c084fc", title: "Vector Search",         tint: "rgba(192, 132, 252, 0.08)" },
  synthesizer:       { dot: "#34d399", title: "Synthesizer",           tint: "rgba(52, 211, 153, 0.08)" },
};

const STRATEGY_LABELS: Record<string, { label: string; color: string; desc: string }> = {
  vector: { label: "Vector RAG", color: "#c084fc", desc: "Semantic similarity over embedded code chunks" },
  graph:  { label: "Graph RAG",  color: "#fbbf24", desc: "Structural query over the Neo4j knowledge graph" },
  hybrid: { label: "Hybrid RAG", color: "#60a5fa", desc: "Graph + Vector retrieval combined" },
};

function StepDetail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-widest text-[var(--fg-4)] mb-0.5">{label}</p>
      <p className={`text-xs leading-relaxed ${mono ? "font-mono text-[var(--fg-2)]" : "text-[var(--fg-2)]"}`}>{value}</p>
    </div>
  );
}

function PipelineStep({ event, isLast, isActive }: { event: NodeEvent; isLast: boolean; isActive: boolean }) {
  const meta = NODE_META[event.node] ?? { dot: "#a1a1aa", title: event.node, tint: "rgba(161,161,170,0.08)" };
  const [open, setOpen] = useState(true);

  const st = event.state ?? {};
  const plan = st.query_plan as Record<string, string> | undefined;
  const strategy = plan?.strategy;
  const sm = strategy ? STRATEGY_LABELS[strategy] : undefined;
  const semanticQuery = plan?.semantic_query;
  const graphHint = plan?.graph_query_hint;

  const vectorChunks = Array.isArray(st.retrieved_vector_data) ? (st.retrieved_vector_data as unknown[]).length : null;
  const graphRecords = Array.isArray(st.retrieved_graph_data) ? (st.retrieved_graph_data as unknown[]).length : null;

  const hasDetails = strategy || (vectorChunks !== null) || (graphRecords !== null) || semanticQuery || graphHint;

  return (
    <div className="flex gap-3">
      {/* Timeline */}
      <div className="flex flex-col items-center flex-shrink-0 pt-1">
        <span className="relative flex items-center justify-center">
          {isActive && (
            <span className="absolute inline-flex h-3 w-3 rounded-full opacity-75 animate-ping" style={{ background: meta.dot }} />
          )}
          <span className="relative w-2 h-2 rounded-full" style={{ background: meta.dot, boxShadow: `0 0 12px ${meta.dot}80` }} />
        </span>
        {!isLast && <span className="w-px flex-1 bg-[var(--border)] mt-1.5 mb-1" />}
      </div>

      {/* Body */}
      <div className="flex-1 pb-3 min-w-0">
        <button
          onClick={() => setOpen(o => !o)}
          className="flex items-center gap-2 w-full text-left group"
        >
          <span className="text-[13px] font-medium text-[var(--fg-1)]">{meta.title}</span>
          {sm && (
            <span className="chip" style={{ background: `${sm.color}10`, borderColor: `${sm.color}40`, color: sm.color }}>
              {sm.label}
            </span>
          )}
          {vectorChunks !== null && (
            <span className="chip" style={{ background: "rgba(192,132,252,0.08)", borderColor: "rgba(192,132,252,0.3)", color: "#c084fc" }}>
              {vectorChunks} chunks
            </span>
          )}
          {graphRecords !== null && graphRecords > 0 && (
            <span className="chip" style={{ background: "rgba(251,191,36,0.08)", borderColor: "rgba(251,191,36,0.3)", color: "#fbbf24" }}>
              {graphRecords} records
            </span>
          )}
          {hasDetails && !isActive && (
            <span className="ml-auto text-[var(--fg-4)] text-xs select-none group-hover:text-[var(--fg-2)] transition-colors">
              {open ? "−" : "+"}
            </span>
          )}
        </button>

        {open && hasDetails && (
          <div
            className="mt-2 rounded-[var(--radius)] border px-3 py-2.5 space-y-2"
            style={{ background: meta.tint, borderColor: `${meta.dot}30` }}
          >
            {sm && <StepDetail label="Strategy" value={`${sm.label} — ${sm.desc}`} />}
            {semanticQuery && <StepDetail label="Semantic query" value={semanticQuery} mono />}
            {graphHint && <StepDetail label="Graph hint" value={graphHint} mono />}
            {vectorChunks !== null && <StepDetail label="Retrieved" value={`${vectorChunks} chunk${vectorChunks !== 1 ? "s" : ""} from Qdrant`} />}
            {graphRecords !== null && graphRecords > 0 && <StepDetail label="Retrieved" value={`${graphRecords} record${graphRecords !== 1 ? "s" : ""} from Neo4j`} />}
          </div>
        )}
      </div>
    </div>
  );
}

export default function StreamingAnswer({ events, isStreaming, finalAnswer }: StreamingAnswerProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const [pipelineOpen, setPipelineOpen] = useState(true);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events, finalAnswer]);

  if (events.length === 0 && !isStreaming && !finalAnswer) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(finalAnswer);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Thinking panel */}
      {(events.length > 0 || isStreaming) && (
        <div className="card overflow-hidden">
          <button
            onClick={() => setPipelineOpen(o => !o)}
            className="w-full flex items-center justify-between px-4 py-3 hover:bg-[var(--surface-3)]/30 transition-colors"
          >
            <div className="flex items-center gap-2">
              <svg className="w-3.5 h-3.5 text-[var(--fg-3)]" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="8" cy="8" r="2"/>
                <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.4 1.4M11.55 11.55l1.4 1.4M3.05 12.95l1.4-1.4M11.55 4.45l1.4-1.4" strokeLinecap="round"/>
              </svg>
              <span className="text-xs font-semibold text-[var(--fg-2)]">Thinking</span>
              {isStreaming && (
                <span className="chip">
                  <span className="status-dot ok" />
                  live
                </span>
              )}
              {!isStreaming && events.length > 0 && (
                <span className="text-[11px] text-[var(--fg-4)]">{events.length} step{events.length !== 1 ? "s" : ""}</span>
              )}
            </div>
            <span className="text-[var(--fg-4)] text-base leading-none select-none">{pipelineOpen ? "−" : "+"}</span>
          </button>

          {pipelineOpen && (
            <div className="px-4 pb-2 pt-2 border-t border-[var(--border)]">
              {isStreaming && events.length === 0 && (
                <div className="flex items-center gap-2 text-[11px] text-[var(--fg-3)] py-3">
                  <svg className="animate-spin w-3 h-3" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25"/>
                    <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
                  </svg>
                  Starting pipeline…
                </div>
              )}
              {events.map((evt, i) => (
                <PipelineStep
                  key={i}
                  event={evt}
                  isLast={i === events.length - 1 && !isStreaming}
                  isActive={isStreaming && i === events.length - 1}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Answer */}
      {finalAnswer && (
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--border)]">
            <div className="flex items-center gap-2">
              <svg className="w-3.5 h-3.5 text-emerald-400" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M8 1.5l1.7 4.5 4.8.4-3.7 3.1 1.2 4.7L8 11.7l-4 2.5 1.2-4.7L1.5 6.4l4.8-.4z" strokeLinejoin="round"/>
              </svg>
              <h3 className="text-xs font-semibold text-[var(--fg-2)]">Answer</h3>
            </div>
            <button
              onClick={handleCopy}
              className="btn-icon"
              title="Copy answer"
              style={{ width: 28, height: 28 }}
            >
              {copied ? (
                <svg className="w-3.5 h-3.5 text-emerald-400" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 8l3 3 7-7" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              ) : (
                <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <rect x="5" y="5" width="9" height="9" rx="1.5"/>
                  <path d="M11 5V3a1 1 0 0 0-1-1H3a1 1 0 0 0-1 1v7a1 1 0 0 0 1 1h2"/>
                </svg>
              )}
            </button>
          </div>
          <div className="answer-prose px-5 py-4 prose prose-invert prose-sm max-w-none
            prose-headings:text-[var(--fg-1)] prose-headings:font-semibold
            prose-h1:text-base prose-h2:text-sm prose-h3:text-sm
            prose-p:text-[var(--fg-2)] prose-p:leading-relaxed
            prose-li:text-[var(--fg-2)] prose-li:leading-relaxed
            prose-strong:text-[var(--fg-1)] prose-strong:font-semibold
            prose-a:text-[var(--accent-hover)] prose-a:no-underline hover:prose-a:underline
            prose-hr:border-[var(--border)]
            prose-blockquote:border-l-[var(--accent)] prose-blockquote:text-[var(--fg-3)] prose-blockquote:italic">
            <ReactMarkdown>{finalAnswer}</ReactMarkdown>
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}

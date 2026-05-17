"use client";

import { useEffect, useState } from "react";
import { API, clearIngestedData } from "@/app/lib/api";

type IngestMode = "github" | "local";

interface IngestResult {
  entities_parsed: number;
  relationships_parsed: number;
  target_path: string;
  latency_ms: number;
  source: string;
}

export default function IngestPanel({
  onDataChange,
  onCleared,
  resetKey = 0,
}: {
  onDataChange?: () => void;
  onCleared?: () => void;
  resetKey?: number;
}) {
  const [mode, setMode] = useState<IngestMode>("github");
  const [githubUrl, setGithubUrl] = useState("");
  const [path, setPath] = useState("./sample_codebase");
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = mode === "github" ? githubUrl.trim().length > 0 : path.trim().length > 0;

  useEffect(() => {
    if (resetKey > 0) {
      setResult(null);
      setError(null);
    }
  }, [resetKey]);

  async function handleClear() {
    if (!confirm("Remove all ingested data from Neo4j and Qdrant?")) return;
    setClearing(true);
    setResult(null);
    setError(null);
    try {
      await clearIngestedData();
      onCleared?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setClearing(false);
    }
  }

  async function handleIngest() {
    setLoading(true);
    setResult(null);
    setError(null);

    const body =
      mode === "github"
        ? { github_url: githubUrl.trim() }
        : { codebase_path: path.trim() };

    try {
      const res = await fetch(`${API}/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const detail = data.detail;
        const message =
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d: { msg?: string }) => d.msg).join(", ")
              : `HTTP ${res.status}`;
        throw new Error(message);
      }
      const data: IngestResult = await res.json();
      setResult(data);
      onDataChange?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <label className="section-label">Source</label>
        {result && (
          <span className="chip chip-success">
            <span className="status-dot ok" /> Ingested
          </span>
        )}
      </div>

      {/* Segmented mode toggle */}
      <div className="segmented" role="tablist">
        <button
          role="tab"
          aria-selected={mode === "github"}
          onClick={() => setMode("github")}
        >
          <svg className="w-3.5 h-3.5 inline mr-1 -mt-0.5" viewBox="0 0 16 16" fill="currentColor">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>
          </svg>
          GitHub
        </button>
        <button
          role="tab"
          aria-selected={mode === "local"}
          onClick={() => setMode("local")}
        >
          <svg className="w-3.5 h-3.5 inline mr-1 -mt-0.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M2 4a1 1 0 0 1 1-1h3l1.5 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4z"/>
          </svg>
          Local
        </button>
      </div>

      {/* Input */}
      {mode === "github" ? (
        <div className="space-y-1.5">
          <input
            type="url"
            value={githubUrl}
            onChange={(e) => setGithubUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            className="input"
          />
          <p className="text-[11px] text-[var(--fg-4)]">
            Public repos. Optional <span className="font-mono text-[var(--fg-3)]">/tree/branch</span>
          </p>
        </div>
      ) : (
        <input
          type="text"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="./sample_codebase"
          className="input font-mono text-xs"
        />
      )}

      {/* Actions */}
      <div className="flex gap-2">
        <button
          onClick={handleIngest}
          disabled={loading || clearing || !canSubmit}
          className="btn-primary flex-1"
        >
          {loading ? (
            <>
              <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none">
                <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25"/>
                <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
              </svg>
              {mode === "github" ? "Cloning…" : "Ingesting…"}
            </>
          ) : (
            <>
              <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M8 2v8M4 6l4-4 4 4M2 13h12" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Ingest
            </>
          )}
        </button>
        <button
          onClick={handleClear}
          disabled={loading || clearing}
          title="Clear ingested data"
          className="btn-danger-subtle"
        >
          {clearing ? (
            <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25"/>
              <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
            </svg>
          ) : (
            <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 4h10M6 4V2.5h4V4M5 4l.6 9a1 1 0 0 0 1 .9h2.8a1 1 0 0 0 1-.9L11 4" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="flex items-start gap-2 rounded-[var(--radius)] border border-red-500/20 bg-red-500/5 px-3 py-2.5">
          <svg className="w-3.5 h-3.5 text-danger flex-shrink-0 mt-0.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="8" cy="8" r="6.5"/>
            <path d="M8 5v3.5M8 11v.5" strokeLinecap="round"/>
          </svg>
          <p className="text-[11px] text-danger leading-relaxed break-all">{error}</p>
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="card-muted p-3 space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <p className="text-[10px] uppercase tracking-widest text-[var(--fg-4)] mb-0.5">Entities</p>
              <p className="text-lg font-semibold text-[var(--fg-1)] leading-none">{result.entities_parsed}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-widest text-[var(--fg-4)] mb-0.5">Edges</p>
              <p className="text-lg font-semibold text-[var(--fg-1)] leading-none">{result.relationships_parsed}</p>
            </div>
          </div>
          <div className="pt-1.5 border-t border-[var(--border)] flex items-center justify-between gap-2">
            <p className="text-[10px] text-[var(--fg-4)] truncate" title={result.source}>{result.source}</p>
            <p className="text-[10px] text-[var(--fg-3)] flex-shrink-0">{result.latency_ms.toFixed(0)} ms</p>
          </div>
        </div>
      )}
    </div>
  );
}

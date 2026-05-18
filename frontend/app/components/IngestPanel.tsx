"use client";

import { useEffect, useState } from "react";
import { API, clearIngestedData } from "@/app/lib/api";

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
  const [githubUrl, setGithubUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [result, setResult] = useState<IngestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = githubUrl.trim().length > 0;

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

    const body = { github_url: githubUrl.trim() };

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
        <label className="section-label">GitHub Source</label>
        {result && (
          <span className="chip chip-success">
            <span className="status-dot ok" /> Ingested
          </span>
        )}
      </div>

      {/* Input */}
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
              Cloning…
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

"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";
import { useTheme } from "@/app/components/ThemeProvider";
import { API, clearIngestedData } from "@/app/lib/api";

function themeColor(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false });

interface GraphNode {
  id: string;
  label: string;
  type: string;
  properties: Record<string, string>;
  x?: number;
  y?: number;
}

interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
  type: string;
}

interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

interface Props {
  refreshKey?: number;
  onCleared?: () => void;
  /** True after a successful ingest in this browser tab — hides the stale-data banner. */
  ingestedThisSession?: boolean;
}

const TYPE_COLORS: Record<string, string> = {
  Module:   "#3b82f6",
  Class:    "#22c55e",
  Function: "#f97316",
};
const TYPE_RADIUS: Record<string, number> = {
  Module:   9,
  Class:    7,
  Function: 4,
};
const RELATION_COLORS: Record<string, string> = {
  IMPORTS: "#818cf8",
  DEFINES: "#34d399",
  CALLS:   "#fb923c",
};

const GRAPH_URL = `${API}/graph?limit=300`;
const GRAPH_HEIGHT_DESKTOP = 460;
const GRAPH_HEIGHT_MOBILE = 340;

const fetcher = async (url: string): Promise<GraphData> => {
  const r = await fetch(url);
  const json = await r.json();
  if (!r.ok) throw new Error(typeof json.detail === "string" ? json.detail : `HTTP ${r.status}`);
  return {
    nodes: Array.isArray(json.nodes) ? json.nodes : [],
    links: Array.isArray(json.links) ? json.links : [],
  };
};

function NodeDetailsPanel({ node, onClose }: { node: GraphNode; onClose: () => void }) {
  const color = TYPE_COLORS[node.type] ?? "#94a3b8";
  const skip = new Set(["qualified_name", "entity_type"]);
  const entries = Object.entries(node.properties).filter(([k]) => !skip.has(k));
  return (
    <div className="w-full flex flex-col bg-[var(--surface-1)]">
      <div className="p-4 border-b border-[var(--border)] flex items-start justify-between gap-2">
        <div className="min-w-0">
          <span className="chip mb-2" style={{ background: color + "20", borderColor: color + "50", color }}>
            {node.type}
          </span>
          <h3 className="text-sm font-semibold text-[var(--fg-1)] break-all leading-snug">{node.label}</h3>
        </div>
        <button
          onClick={onClose}
          className="btn-icon"
          style={{ width: 26, height: 26 }}
        >
          <svg className="w-3 h-3" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M3 3l10 10M13 3L3 13" strokeLinecap="round"/>
          </svg>
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {entries.length === 0 && <p className="text-xs text-[var(--fg-4)]">No additional properties.</p>}
        {entries.map(([k, v]) => (
          <div key={k}>
            <p className="text-[10px] uppercase tracking-widest text-[var(--fg-4)] mb-0.5">{k.replace(/_/g, " ")}</p>
            <p className="text-xs text-[var(--fg-2)] break-all font-mono leading-relaxed">{v || "—"}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function GraphVisualization({
  refreshKey = 0,
  onCleared,
  ingestedThisSession = false,
}: Props) {
  const { theme } = useTheme();
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);
  const [graphBg, setGraphBg] = useState("#0a0a0c");
  const [showStaleBanner, setShowStaleBanner] = useState(false);
  const initialGraphLoadDone = useRef(false);

  useEffect(() => {
    setGraphBg(themeColor("--graph-bg", "#0a0a0c"));
  }, [theme]);

  const { data, error, isLoading, mutate } = useSWR<GraphData>(
    [GRAPH_URL, refreshKey],
    ([url]) => fetcher(url as string),
    { revalidateOnMount: true, revalidateOnFocus: false, dedupingInterval: 0 }
  );

  useEffect(() => {
    if (ingestedThisSession) setShowStaleBanner(false);
  }, [ingestedThisSession]);

  useEffect(() => {
    if (isLoading || error || !data || initialGraphLoadDone.current) return;
    initialGraphLoadDone.current = true;
    if (data.nodes.length > 0 && !ingestedThisSession) {
      setShowStaleBanner(true);
    }
  }, [data, isLoading, error, ingestedThisSession]);

  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const [search, setSearch] = useState("");
  const [activeType, setActiveType] = useState<string | null>(null);
  const [showFunctions, setShowFunctions] = useState(false);
  const [showLabels, setShowLabels] = useState(false);
  const [graphWidth, setGraphWidth] = useState(0);
  const canvasHostRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<{
    zoomToFit: (ms: number, padding: number) => void;
    d3Force: (name: string, force?: unknown) => unknown;
    pauseAnimation: () => void;
    resumeAnimation: () => void;
  } | null>(null);

  const initialLayoutDone = useRef(false);

  useEffect(() => {
    initialLayoutDone.current = false;
    fgRef.current?.resumeAnimation();
  }, [data, refreshKey, showFunctions]);

  const zoomToFit = useCallback(() => {
    fgRef.current?.zoomToFit(400, 48);
  }, []);

  const graphHeight = graphWidth > 0 && graphWidth < 640 ? GRAPH_HEIGHT_MOBILE : GRAPH_HEIGHT_DESKTOP;

  const handleEngineStop = useCallback(() => {
    if (!initialLayoutDone.current) {
      initialLayoutDone.current = true;
      zoomToFit();
    }
    // Freeze layout so nodes stop drifting; drag still works via fixed positions.
    fgRef.current?.pauseAnimation();
  }, [zoomToFit]);

  useEffect(() => {
    const el = canvasHostRef.current;
    if (!el) return;
    let timeout: ReturnType<typeof setTimeout>;
    const measure = () => {
      clearTimeout(timeout);
      timeout = setTimeout(() => {
        const w = el.clientWidth;
        if (w > 0) setGraphWidth(w);
      }, 200);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      clearTimeout(timeout);
      ro.disconnect();
    };
  }, []);

  useEffect(() => {
    if (!fgRef.current) return;
    const fg = fgRef.current;
    const charge = fg.d3Force("charge");
    if (charge && typeof (charge as { strength?: (v: number) => unknown }).strength === "function") {
      (charge as { strength: (v: number) => unknown }).strength(-800);
    }
    const link = fg.d3Force("link");
    if (link && typeof (link as { distance?: (v: number) => unknown }).distance === "function") {
      (link as { distance: (v: number) => unknown }).distance(120);
    }
  }, [data]);

  const allNodes = useMemo(() => data?.nodes ?? [], [data?.nodes]);
  const allLinks = useMemo(() => data?.links ?? [], [data?.links]);

  const selectedId = selectedNode?.id ?? null;

  const matchedIds = useMemo(
    () =>
      new Set(
        allNodes
          .filter(n => {
            const functionOk = showFunctions || n.type !== "Function";
            const typeOk = activeType ? n.type === activeType : true;
            const searchOk = search
              ? n.label.toLowerCase().includes(search.toLowerCase()) ||
                Object.values(n.properties).some(v => String(v).toLowerCase().includes(search.toLowerCase()))
              : true;
            return functionOk && typeOk && searchOk;
          })
          .map(n => n.id)
      ),
    [allNodes, activeType, search, showFunctions]
  );

  // Keep a stable node/link set for the physics engine (only changes on ingest/refresh).
  // Search and type filters use visibility instead of removing nodes — avoids re-simulation jitter.
  const simulationNodes = useMemo(
    () => allNodes.filter(n => showFunctions || n.type !== "Function"),
    [allNodes, showFunctions]
  );
  const simulationNodeIds = useMemo(
    () => new Set(simulationNodes.map(n => n.id)),
    [simulationNodes]
  );
  const simulationLinks = useMemo(
    () =>
      allLinks.filter(l => {
        const s = typeof l.source === "object" ? l.source.id : l.source;
        const t = typeof l.target === "object" ? l.target.id : l.target;
        return simulationNodeIds.has(s) && simulationNodeIds.has(t);
      }),
    [allLinks, simulationNodeIds]
  );
  const graphData = useMemo(
    () => ({ nodes: simulationNodes, links: simulationLinks }),
    [simulationNodes, simulationLinks]
  );

  const displayNodeCount = matchedIds.size;
  const displayLinkCount = useMemo(
    () =>
      simulationLinks.filter(l => {
        const s = typeof l.source === "object" ? l.source.id : l.source;
        const t = typeof l.target === "object" ? l.target.id : l.target;
        return matchedIds.has(s) && matchedIds.has(t);
      }).length,
    [simulationLinks, matchedIds]
  );

  const isNodeVisible = useCallback(
    (node: object) => matchedIds.has((node as GraphNode).id),
    [matchedIds]
  );

  const isLinkVisible = useCallback(
    (link: object) => {
      const l = link as GraphLink;
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return matchedIds.has(s) && matchedIds.has(t);
    },
    [matchedIds]
  );

  const selectedNeighborIds = useMemo(() => {
    if (!selectedId) return new Set<string>();
    const neighbors = new Set<string>([selectedId]);
    for (const link of simulationLinks) {
      const s = typeof link.source === "object" ? link.source.id : link.source;
      const t = typeof link.target === "object" ? link.target.id : link.target;
      if (s === selectedId) neighbors.add(t);
      if (t === selectedId) neighbors.add(s);
    }
    return neighbors;
  }, [simulationLinks, selectedId]);

  const nodePointerAreaPaint = useCallback(
    (node: object, color: string, ctx: CanvasRenderingContext2D) => {
      const n = node as GraphNode;
      const r = (TYPE_RADIUS[n.type] ?? 4) + 6;
      ctx.beginPath();
      ctx.arc(n.x ?? 0, n.y ?? 0, r, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    []
  );

  const nodeLabel = useCallback((node: object) => {
    const n = node as GraphNode;
    const short = n.label.split(".").pop() ?? n.label;
    return `${short} · ${n.type}`;
  }, []);

  const paintNode = useCallback(
    (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const n = node as GraphNode;
      const r = TYPE_RADIUS[n.type] ?? 4;
      const color = TYPE_COLORS[n.type] ?? "#94a3b8";
      const isSelected = selectedNode?.id === n.id;
      const isHovered = hoveredNode?.id === n.id;
      const isSearchMatch = search.length > 0 && matchedIds.has(n.id);
      const hasSelection = Boolean(selectedId);
      const isRelatedToSelection = selectedNeighborIds.has(n.id);
      const isDimmed = hasSelection && !isRelatedToSelection;

      ctx.beginPath();
      ctx.arc(n.x ?? 0, n.y ?? 0, isSelected ? r * 1.5 : r, 0, 2 * Math.PI);
      ctx.fillStyle = isDimmed
        ? themeColor("--graph-hover-ring", "rgba(148,163,184,0.2)")
        : isSelected
          ? themeColor("--graph-label-active", "#ffffff")
          : isSearchMatch
            ? "#facc15"
            : color;
      ctx.fill();

      if (isSelected) {
        ctx.beginPath();
        ctx.arc(n.x ?? 0, n.y ?? 0, r * 1.7, 0, 2 * Math.PI);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      const showLabel =
        isHovered ||
        isSelected ||
        isSearchMatch ||
        showLabels ||
        globalScale > 3.2 ||
        (n.type === "Module" && globalScale > 1.8);
      if (showLabel) {
        const shortLabel = n.label.split(".").pop() ?? n.label;
        const fontSize = Math.max(9, Math.min(13, 11 / globalScale * 1.8));
        const x = n.x ?? 0;
        const y = (n.y ?? 0) + r + 3;
        ctx.font = `${isSelected || isHovered ? "600 " : ""}${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const textW = ctx.measureText(shortLabel).width;
        if (isHovered || isSelected) {
          ctx.fillStyle = themeColor("--graph-label-bg", "rgba(10, 10, 12, 0.92)");
          ctx.fillRect(x - textW / 2 - 5, y - 2, textW + 10, fontSize + 6);
        }
        ctx.fillStyle = isDimmed
          ? themeColor("--graph-hover-ring", "rgba(148,163,184,0.45)")
          : isSelected || isHovered
            ? themeColor("--graph-label-active", "#ffffff")
            : themeColor("--graph-label-text", "#e2e8f0");
        ctx.fillText(shortLabel, x, y);
      }
    },
    [selectedNode, hoveredNode, search, matchedIds, selectedId, selectedNeighborIds, showLabels, theme]
  );

  const handleNodeHover = useCallback((node: object | null) => {
    setHoveredNode(node ? (node as GraphNode) : null);
  }, []);

  const handleNodeClick = useCallback((node: object) => {
    const n = node as GraphNode;
    setSelectedNode(prev => prev?.id === n.id ? null : n);
  }, []);

  const linkColor = useCallback(
    (link: object) => {
      const l = link as GraphLink;
      const base = RELATION_COLORS[l.type] ?? "#374151";
      if (!selectedId) return `${base}AA`;
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return s === selectedId || t === selectedId ? base : `${base}33`;
    },
    [selectedId]
  );

  const linkWidth = useCallback(
    (link: object) => {
      if (!selectedId) return 0.8;
      const l = link as GraphLink;
      const s = typeof l.source === "object" ? l.source.id : l.source;
      const t = typeof l.target === "object" ? l.target.id : l.target;
      return s === selectedId || t === selectedId ? 1.6 : 0.4;
    },
    [selectedId]
  );

  if (isLoading) {
    return (
      <div className="card flex items-center justify-center h-60 text-[var(--fg-3)] text-sm gap-2">
        <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25"/>
          <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
        </svg>
        Loading knowledge graph…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="card flex flex-col items-center justify-center h-60 gap-1.5 text-sm">
        <p className="text-red-400 text-xs">{error ? String(error) : "No graph data."}</p>
        <p className="text-[var(--fg-4)] text-[11px]">Ingest a codebase first.</p>
      </div>
    );
  }
  if (allNodes.length === 0) {
    return (
      <div className="card flex flex-col items-center justify-center h-60 gap-2">
        <svg className="w-6 h-6 text-[var(--fg-4)]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="6" cy="6" r="2"/>
          <circle cx="18" cy="6" r="2"/>
          <circle cx="12" cy="18" r="2"/>
          <path d="M8 6h8M7 7l4 9M17 7l-4 9"/>
        </svg>
        <p className="text-[var(--fg-2)] text-sm">Knowledge graph is empty</p>
        <p className="text-[var(--fg-4)] text-[11px]">Ingest a codebase to populate it.</p>
      </div>
    );
  }

  const typeCounts: Record<string, number> = {};
  for (const n of allNodes) typeCounts[n.type] = (typeCounts[n.type] ?? 0) + 1;

  async function handleBannerClear() {
    if (!confirm("Remove all ingested data from Neo4j and Qdrant?")) return;
    setClearing(true);
    setClearError(null);
    try {
      await clearIngestedData();
      setSelectedNode(null);
      await mutate({ nodes: [], links: [] }, { revalidate: true });
      setShowStaleBanner(false);
      onCleared?.();
    } catch (err) {
      setClearError(err instanceof Error ? err.message : String(err));
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="flex flex-col gap-2.5">
      {showStaleBanner && (
        <>
          <div className="banner-persist flex flex-wrap items-center justify-between gap-2 px-3 py-2.5">
            <div className="flex items-start gap-2 min-w-0">
              <svg className="w-4 h-4 text-warning flex-shrink-0 mt-0.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="8" cy="8" r="6.5"/>
                <path d="M8 5v1.5M8 10.5v.5" strokeLinecap="round"/>
              </svg>
              <p className="text-xs leading-relaxed">
                <span className="banner-persist-title">Showing data from a previous ingest</span>
                <span className="banner-persist-body"> — stored on the server. Clear before ingesting a different repo.</span>
              </p>
            </div>
            <button
              type="button"
              onClick={() => void handleBannerClear()}
              disabled={clearing}
              className="btn-danger-subtle text-xs py-1.5 px-2.5 flex-shrink-0"
            >
              {clearing ? (
                <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" opacity="0.25"/>
                  <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="3" strokeLinecap="round"/>
                </svg>
              ) : (
                "Clear"
              )}
            </button>
          </div>
          {clearError && (
            <p className="text-[11px] text-danger px-1">{clearError}</p>
          )}
        </>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-0">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--fg-4)] pointer-events-none" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="7" cy="7" r="5"/>
            <path d="M11 11l3 3" strokeLinecap="round"/>
          </svg>
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search nodes…"
            className="input pl-8 py-1.5 text-xs"
          />
        </div>

        <div className="flex gap-1.5 flex-wrap">
          {Object.entries(TYPE_COLORS).map(([type, color]) => (
            <button
              key={type}
              onClick={() => setActiveType(t => t === type ? null : type)}
              className="chip transition-all"
              style={
                activeType === type
                  ? { background: color + "20", borderColor: color + "60", color }
                  : {}
              }
            >
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
              {type}
              <span className="opacity-60 ml-0.5">{typeCounts[type] ?? 0}</span>
            </button>
          ))}
        </div>

        <div className="flex gap-1.5 flex-wrap">
          <button
            onClick={() => setShowFunctions(v => !v)}
            className="chip transition-all"
            style={showFunctions ? { borderColor: "#f97316AA", color: "#fdba74", background: "rgba(249,115,22,0.14)" } : {}}
          >
            Functions {showFunctions ? "on" : "off"}
          </button>
          <button
            onClick={() => setShowLabels(v => !v)}
            className="chip transition-all"
            style={
              showLabels
                ? {
                    borderColor: "var(--border-strong)",
                    color: "var(--fg-1)",
                    background: "var(--surface-3)",
                  }
                : {}
            }
          >
            Labels {showLabels ? "on" : "off"}
          </button>
        </div>

        <div className="flex gap-1">
          <button onClick={zoomToFit} title="Zoom to fit" className="btn-icon">
            <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4" strokeLinecap="round"/>
            </svg>
          </button>
          <button onClick={() => { mutate(); setSelectedNode(null); }} title="Refresh" className="btn-icon">
            <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M13.5 8A5.5 5.5 0 1 1 8 2.5M13.5 2.5v3h-3" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>
      </div>

      <div className="card relative w-full overflow-hidden" style={{ height: graphHeight, background: "var(--graph-bg)" }}>
        <div ref={canvasHostRef} className="relative w-full h-full">
          <div className="absolute top-3 left-3 z-10 flex gap-1.5 pointer-events-none">
            <span className="chip" style={{ background: "var(--graph-chip-bg)", backdropFilter: "blur(8px)" }}>
              {displayNodeCount} nodes
            </span>
            <span className="chip" style={{ background: "var(--graph-chip-bg)", backdropFilter: "blur(8px)" }}>
              {displayLinkCount} edges
            </span>
            {(search || activeType) && (
              <button
                type="button"
                onClick={() => { setSearch(""); setActiveType(null); }}
                className="chip cursor-pointer hover:border-[var(--border-strong)] pointer-events-auto"
                style={{ background: "rgba(245,158,11,0.1)", borderColor: "rgba(245,158,11,0.3)", color: "#fbbf24" }}
              >
                ✕ clear filter
              </button>
            )}
          </div>

          <div
            className="absolute bottom-3 left-3 z-10 flex flex-wrap gap-2 px-2.5 py-1.5 rounded-[var(--radius)] pointer-events-none max-w-[calc(100%-1.5rem)]"
            style={{ background: "var(--graph-chip-bg)", backdropFilter: "blur(8px)", border: "1px solid var(--border)" }}
          >
            {Object.entries(RELATION_COLORS).map(([type, color]) => (
              <span key={type} className="flex items-center gap-1.5 text-[10px] text-[var(--fg-3)]">
                <span className="inline-block w-4 h-0.5 rounded-full" style={{ background: color }} />
                {type}
              </span>
            ))}
          </div>

          <ForceGraph2D
            // @ts-expect-error ref typing mismatch
            ref={fgRef}
            width={graphWidth || 800}
            height={graphHeight}
            graphData={graphData}
            backgroundColor={graphBg}
            nodeCanvasObject={paintNode}
            nodeCanvasObjectMode={() => "replace"}
            nodePointerAreaPaint={nodePointerAreaPaint}
            nodeVisibility={isNodeVisible}
            linkVisibility={isLinkVisible}
            nodeRelSize={1}
            linkColor={linkColor}
            linkDirectionalArrowLength={4}
            linkDirectionalArrowRelPos={1}
            linkWidth={linkWidth}
            nodeLabel={nodeLabel}
            onNodeHover={handleNodeHover}
            onNodeClick={handleNodeClick}
            onBackgroundClick={() => {
              setSelectedNode(null);
              setHoveredNode(null);
            }}
            warmupTicks={80}
            cooldownTicks={60}
            d3AlphaDecay={0.08}
            d3VelocityDecay={0.55}
            onEngineStop={handleEngineStop}
          />
        </div>
      </div>

      {selectedNode && (
        <div className="card overflow-hidden">
          <NodeDetailsPanel node={selectedNode} onClose={() => setSelectedNode(null)} />
        </div>
      )}
    </div>
  );
}

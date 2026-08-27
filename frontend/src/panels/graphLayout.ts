/**
 * Deterministic layered layout for the Graph panel (PRD §28.3: "Layout is deterministic
 * (dagre with a fixed seed) so the graph does not reshuffle between runs").
 *
 * **Deviation from the PRD's literal "dagre"** (declared, CONTEXT.md ruling **D-59**, not
 * silent): `dagre` is not in the locked dependency set (CONTEXT.md §3) or `package.json`, and
 * AGENTS.md §2 requires an ADR — logged in CONTEXT.md §8 — before any new dependency enters
 * `package.json`, with no human available in this session to approve one before code needed
 * to ship. This module implements the same *property* the PRD actually cares about — a
 * layered, top-to-bottom DAG layout that is a pure function of the graph's own structure, so
 * two loads of the same run produce byte-identical positions and the graph never reshuffles —
 * without a new dependency. `dagre` itself (or `@dagrejs/dagre`) is a drop-in swap if the
 * owner ratifies the ADR; nothing about `GraphPanel.tsx` depends on this specific algorithm.
 *
 * Algorithm: BFS layering from every source node (no incoming edge) breaks ties by sorted
 * agent id (never insertion order — NFR-14-style determinism); a node reachable from multiple
 * layers takes the deepest (longest-path layering, the same shape dagre's own default
 * `longest-path` ranker produces, which keeps an edge from skipping backward across layers).
 * Within a layer, nodes are ordered by id, sorted — no heuristic crossing-minimisation (dagre's
 * `order` phase); acceptable for the graph sizes this panel targets (Design Constraint 4: up
 * to ~200 nodes) and, unlike a randomised or insertion-order layout, still fully deterministic.
 */
import type { GraphEdge, GraphNode } from '../store/types';

export interface NodePosition {
  id: string;
  x: number;
  y: number;
}

export interface GraphLayout {
  positions: Map<string, NodePosition>;
  width: number;
  height: number;
}

const COLUMN_WIDTH = 220;
const ROW_HEIGHT = 110;
const PADDING = 40;

export function layoutGraph(nodes: readonly GraphNode[], edges: readonly GraphEdge[]): GraphLayout {
  const ids = [...nodes.map((n) => n.id)].sort();
  const outgoing = new Map<string, string[]>();
  const incomingCount = new Map<string, number>();
  for (const id of ids) {
    outgoing.set(id, []);
    incomingCount.set(id, 0);
  }
  for (const edge of edges) {
    if (!outgoing.has(edge.from) || !incomingCount.has(edge.to)) continue; // defensive
    outgoing.get(edge.from)!.push(edge.to);
    incomingCount.set(edge.to, (incomingCount.get(edge.to) ?? 0) + 1);
  }
  for (const list of outgoing.values()) list.sort();

  // Longest-path layering via a stable topological relaxation. Falls back to "no incoming
  // edges yet processed" sources first; a cyclic graph (which this build's fixtures never
  // produce, but a future one might) still terminates because each edge relaxes a layer at
  // most `nodes.length` times before the bound below stops it.
  const layer = new Map<string, number>();
  for (const id of ids) layer.set(id, 0);
  const sources = ids.filter((id) => (incomingCount.get(id) ?? 0) === 0);
  const queue = [...(sources.length > 0 ? sources : ids)];
  let iterations = 0;
  const maxIterations = ids.length * ids.length + ids.length;
  while (queue.length > 0 && iterations < maxIterations) {
    iterations += 1;
    const id = queue.shift();
    if (id === undefined) break;
    const current = layer.get(id) ?? 0;
    for (const next of outgoing.get(id) ?? []) {
      const proposed = current + 1;
      if (proposed > (layer.get(next) ?? 0)) {
        layer.set(next, proposed);
        queue.push(next);
      }
    }
  }

  const byLayer = new Map<number, string[]>();
  for (const id of ids) {
    const l = layer.get(id) ?? 0;
    const bucket = byLayer.get(l);
    if (bucket) bucket.push(id);
    else byLayer.set(l, [id]);
  }
  for (const bucket of byLayer.values()) bucket.sort();

  const positions = new Map<string, NodePosition>();
  let maxRowLength = 1;
  for (const [l, rowIds] of [...byLayer.entries()].sort((a, b) => a[0] - b[0])) {
    maxRowLength = Math.max(maxRowLength, rowIds.length);
    rowIds.forEach((id, index) => {
      positions.set(id, {
        id,
        x: PADDING + l * COLUMN_WIDTH,
        y: PADDING + index * ROW_HEIGHT,
      });
    });
  }

  const width = PADDING * 2 + (byLayer.size || 1) * COLUMN_WIDTH;
  const height = PADDING * 2 + maxRowLength * ROW_HEIGHT;
  return { positions, width, height };
}

/** Design Constraint 4: React Flow to ~200 nodes (locked stack) — report, never silently
 * swap charting libraries, past this ceiling. */
export const REACT_FLOW_NODE_CEILING = 200;

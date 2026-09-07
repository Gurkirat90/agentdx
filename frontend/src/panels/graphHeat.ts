/**
 * Edge colour = latency, the heat ramp (PRD §28.3, §29.1: "fast -> slow: sage-green - sage -
 * cream - amber - clay - clay-red", six `--heat-0`..`--heat-5` tokens already defined in
 * `tokens.css`). This module maps one `GraphEdge`'s own handoff latency to one of the six
 * ramp steps — never a literal hex (AGENTS.md §4): every step is a CSS custom property name,
 * resolved by the component's stylesheet, matching `waterfallBuckets.ts`'s established
 * pattern of a pure `data -> token name` function with no colour value inside it.
 *
 * Ruling (PRD-silent point, **C-35**): the PRD names the six ramp steps but no formula for
 * how a set of edges' own latencies maps onto them — no absolute millisecond thresholds are
 * named anywhere. `heatStepsForEdges` below buckets by rank (quantile) among the run's own
 * edges, not fixed thresholds — the same relative-scale treatment `waterfallBuckets.ts`/C-30
 * already established for a different panel; "fast"/"slow" are meaningful only relative to
 * what this run actually produced.
 */
import type { GraphEdge } from '../store/types';

export type HeatStep = 0 | 1 | 2 | 3 | 4 | 5;

/** Mean handoff latency per message — the only per-edge latency figure `GraphEdge` actually
 * carries (`total_handoff_ms / messages`); `cp_handoff_ms`/`cp_share` measure critical-path
 * contribution, a different axis PRD §28.3 reserves for edge *width*, not colour. */
export function meanHandoffMs(edge: GraphEdge): number {
  return edge.messages > 0 ? edge.total_handoff_ms / edge.messages : 0;
}

/**
 * Buckets edges into six ramp steps by rank among the given edge set (quantile buckets, not
 * fixed millisecond thresholds) — PRD §28.3 names no absolute latency scale for the ramp, and
 * a fixed-ms threshold picked without one would be an invented number this build cannot back
 * with a PRD citation. Ranking against the run's own edges is the same relative-scale
 * treatment `visx/scale` gives the waterfall's x-axis: "fast" and "slow" are meaningful only
 * relative to what this run actually produced.
 */
export function heatStepsForEdges(edges: readonly GraphEdge[]): Map<GraphEdge, HeatStep> {
  const sorted = [...edges].sort((a, b) => meanHandoffMs(a) - meanHandoffMs(b));
  const steps = new Map<GraphEdge, HeatStep>();
  const n = sorted.length;
  sorted.forEach((edge, index) => {
    const step = n <= 1 ? 0 : Math.min(5, Math.floor((index / n) * 6));
    steps.set(edge, step as HeatStep);
  });
  return steps;
}

export function heatToken(step: HeatStep): string {
  return `heat-${step}`;
}

/** Edge width from message volume (PRD §28.3: "edge width = message volume"), clamped to a
 * readable range — an unbounded linear width would make a single 200-message edge unreadable
 * next to a 1-message one on the same canvas. */
export function widthForMessages(messages: number, maxMessages: number): number {
  if (maxMessages <= 0) return 1.5;
  const ratio = messages / maxMessages;
  return 1.5 + ratio * 6.5; // 1.5px .. 8px
}

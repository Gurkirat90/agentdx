import type { WaterfallSpan } from '../store/types';
import type { WaterfallViewport } from './useWaterfallViewport';

/**
 * NFR-3 perf fix: `LaneRow` used to `Array.prototype.filter` every span in a lane on every
 * scroll-driven viewport update — O(spans) work, 24 times over, on every rAF tick, even though
 * a real backend always emits one agent's spans in temporal order (each span starts after the
 * previous one ends — `agentdx.api.routes.analysis.get_waterfall` builds `WaterfallLane.spans`
 * from the store's own seq-ordered event stream). A Playwright trace at 5 280 spans/24 lanes
 * (tests/frontend/e2e/perf.spec.ts) measured this filter as the dominant cost keeping frame
 * rate below NFR-3's 60fps floor during a scroll sweep. Binary-searching the (already sorted)
 * lane for the viewport's lower edge, then scanning forward only through the spans actually in
 * view, turns that into O(log n + k) — k being the visible count, not the lane's total.
 */
export function visibleSpans(spans: readonly WaterfallSpan[], viewport: WaterfallViewport): WaterfallSpan[] {
  const { startMs, endMs } = viewport;
  // Lower bound: first index whose span could possibly intersect startMs. Spans are
  // start_ms-ordered but not necessarily end_ms-ordered against a single scalar (a span can
  // be long), so back up from the start_ms lower bound rather than assuming end_ms tracks it —
  // cheap in practice since spans are short relative to the viewport window.
  let lo = 0;
  let hi = spans.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    const span = spans[mid];
    if (span !== undefined && span.start_ms < startMs) lo = mid + 1;
    else hi = mid;
  }

  // `lo` is the first span whose start_ms >= startMs. A span *before* `lo` can still overlap
  // the viewport if it started earlier but hasn't ended yet — walk back while that holds.
  let from = lo;
  while (from > 0) {
    const prev = spans[from - 1];
    if (prev !== undefined && prev.end_ms >= startMs) from -= 1;
    else break;
  }

  const out: WaterfallSpan[] = [];
  for (let i = from; i < spans.length; i++) {
    const span = spans[i];
    if (span === undefined) continue;
    if (span.start_ms > endMs) break; // spans are start_ms-ordered — nothing further qualifies
    if (span.end_ms >= startMs) out.push(span);
  }
  return out;
}

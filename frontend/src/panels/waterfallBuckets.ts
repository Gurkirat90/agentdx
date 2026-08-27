/**
 * Bucket → fill mapping for the waterfall (PRD §28.3: "bars segmented by overhead bucket
 * with distinct fills: █ work · ░ blocking wait · ▓ handoff · ▒ retry").
 *
 * Ruling (PRD-silent point, not guessed at): §28.3 names exactly four fills for the
 * waterfall specifically; §29.1's heat ramp is described for "graph edges and waterfall
 * bars" in general, but the graph panel (out of this prompt's scope) is the ramp's literal
 * use case (edge colour = latency) and the waterfall's own worked example (§29.3's ASCII
 * mockup) shows the four-glyph bucket fill, not a latency gradient. This module implements
 * §28.3's four-fill spec for span bars; a future prompt adding a latency-coloured view is a
 * genuinely separate feature, not a fix to this one.
 *
 * The real `GET .../waterfall` backend always returns `bucket: null` today (declared gap,
 * `analysis.overhead._classify_node` has no public per-node surface yet, P17 scope) — every
 * span with a `null`/unrecognised bucket renders the neutral `work` fill, never a fabricated
 * classification.
 */
export type WaterfallBucketFill = 'work' | 'blocking_wait' | 'handoff' | 'retry';

const BUCKET_TO_FILL: Record<string, WaterfallBucketFill> = {
  productive_work: 'work',
  orchestration: 'work',
  redundant_work: 'work',
  blocking_wait: 'blocking_wait',
  handoff: 'handoff',
  retry_recovery: 'retry',
};

export function fillForBucket(bucket: string | null): WaterfallBucketFill {
  if (bucket === null) return 'work';
  return BUCKET_TO_FILL[bucket] ?? 'work';
}

export const BUCKET_LEGEND: { fill: WaterfallBucketFill; glyph: string; label: string }[] = [
  { fill: 'work', glyph: '█', label: 'work' },
  { fill: 'blocking_wait', glyph: '░', label: 'blocking wait' },
  { fill: 'handoff', glyph: '▓', label: 'handoff' },
  { fill: 'retry', glyph: '▒', label: 'retry' },
];

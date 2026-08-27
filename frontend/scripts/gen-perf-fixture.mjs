#!/usr/bin/env node
/**
 * Generates a synthetic, deterministic `WaterfallResponse`-shaped fixture with >5 000 spans,
 * for the NFR-3 perf test (`npm run test:perf`) only — the DoD names this explicitly as a
 * *synthetic* stress fixture, unlike the three golden fixtures (`tests/frontend/fixtures/
 * *.waterfall.json`), which are real backend output from committed event logs. This file
 * exists purely to exercise the >5 000-span virtualisation path (Waterfall.tsx's
 * `VIRTUALIZE_THRESHOLD`); it is never presented as, or mixed with, real telemetry.
 *
 * The shape mirrors the real `get_waterfall` route's response exactly (span_id, kind, name,
 * start_ms/end_ms, bucket, on_critical_path, status, fault_id, seq_start/seq_end) so the
 * Waterfall panel renders it through the identical code path a real 5 000-span run would use.
 */
import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const outPath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../tests/frontend/fixtures/perf_5000_spans.waterfall.json',
);

const LANES = 24;
const SPANS_PER_LANE = 220; // 24 * 220 = 5280, safely above the 5 000-span threshold
const AGENTS = Array.from({ length: LANES }, (_, i) => `agent-${String(i).padStart(2, '0')}`);
const KINDS = ['llm_call', 'tool_call', 'agent_step'];
const BUCKETS = [null, 'blocking_wait', 'handoff', 'retry_recovery', 'orchestration', 'redundant_work'];

// Deterministic PRNG (mulberry32) — same output on every run, no external dependency.
function mulberry32(seed) {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry32(0x5eed_5eed);

// Gaps are deliberately large relative to span duration: a real multi-agent run's 5 000+
// spans stretch across tens of seconds of virtual time (tool/network latency between calls),
// not packed edge-to-edge into a couple of screen-widths. This matters for the perf test
// specifically — with a short, densely-packed timeline, Waterfall.tsx's viewport-plus-margin
// window (useWaterfallViewport's MARGIN_FACTOR) can cover almost the *entire* timeline at
// once regardless of scroll position, so the >5 000-span virtualisation path barely
// filters anything and the perf test would only ever be measuring the unvirtualised case.
let seq = 0;
const lanes = AGENTS.map((agent, laneIdx) => {
  let cursorMs = laneIdx * 30;
  const spans = [];
  for (let i = 0; i < SPANS_PER_LANE; i++) {
    const durationMs = 5 + Math.floor(rand() * 60);
    const startMs = cursorMs;
    const endMs = startMs + durationMs;
    const kind = KINDS[Math.floor(rand() * KINDS.length)];
    const bucket = BUCKETS[Math.floor(rand() * BUCKETS.length)];
    const onCriticalPath = laneIdx === 0 && i % 5 === 0; // one lane carries a visible critical path
    spans.push({
      span_id: `perf_${laneIdx}_${i}`,
      kind,
      name: `${kind}_${i}`,
      start_ms: startMs,
      end_ms: endMs,
      bucket,
      on_critical_path: onCriticalPath,
      status: 'ok',
      fault_id: null,
      seq_start: seq++,
      seq_end: seq++,
    });
    cursorMs = endMs + 100 + Math.floor(rand() * 300);
  }
  return { agent, spans };
});

const virtualMakespanMs = Math.max(...lanes.flatMap((l) => l.spans.map((s) => s.end_ms)));

const payload = {
  run_id: 'r_perf_synthetic',
  fixture: 'synthetic_perf_5000',
  waterfall: {
    virtual_makespan_ms: virtualMakespanMs,
    // Matches the real backend's current always-null behaviour (§ GhostBaseline.tsx) — the
    // perf test exercises the pending-ghost render path, same as it would against a real run.
    baseline_makespan_ms: null,
    lanes,
  },
};

const totalSpans = lanes.reduce((n, l) => n + l.spans.length, 0);
writeFileSync(outPath, JSON.stringify(payload, null, 2));
console.log(`gen:fixtures — wrote ${totalSpans} spans across ${lanes.length} lanes to ${path.relative(process.cwd(), outPath)}`);

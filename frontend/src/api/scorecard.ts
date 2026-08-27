/**
 * `ScorecardResponse` (the GENERATED type, `./schema.ts`) is `{ [key: string]: unknown }` —
 * deliberately untyped. `src/agentdx/api/routes/scorecard.py`'s own docstring says why: "the
 * route serialises what the analysis pipeline wrote, and inventing a stricter schema here
 * would be a second, competing opinion of what a scorecard contains." No prompt has yet built
 * the pipeline that calls `store.upsert_scorecard` (P17 gap, `src/agentdx/store/sqlite.py`'s
 * `ScorecardRecord.payload`), so there is no shipped example of the real JSON to type against.
 *
 * This file is therefore a documented, narrow ruling — not a guess — for the shape the panel
 * expects once that pipeline exists, derived from two ground-truth sources rather than
 * invented: (1) `analysis/baseline.py`'s real `BaselineComparison` / `BucketAttribution` /
 * `ComparabilityAssessment` dataclasses, the actual data a scorecard is computed from; (2)
 * `scorecard.py`'s own docstring, which names the five top-level keys the persisted payload
 * uses: `speedup{}`, `buckets[]`, `tokens{}`, `comparability{}`, `resilience{}`. PRD §17.4's
 * printed block is the third source — every field below maps to a line in it.
 *
 * `resilience` and `wall_makespan_ms` are optional: PRD §17.4's own worked example prints a
 * "Wall time" line and `analysis/resilience.py` exists, but neither is a `BaselineComparison`
 * field, and no field name for either is fixed anywhere yet — the panel renders them only
 * when present, never fabricates them (PRD §29.8, I9). If a real pipeline ships a payload
 * that doesn't match this shape, `parseScorecardPayload` returns `null` and the panel shows
 * an honest "scorecard payload not in the expected shape" state rather than guessing at
 * fields — never a silent partial render of mismatched data.
 */
import type { components } from './schema';

export type ScorecardResponse = components['schemas']['ScorecardResponse'];

export interface ScorecardBucket {
  bucket: string;
  critical_path_ms: number;
  gap_contribution: number;
  evidence_seq: number[];
}

export interface ScorecardComparability {
  grade: 'A' | 'B' | 'C';
  cache_reuse_rate: number;
  cache_reuse_tool_rate: number;
  cache_reuse_llm_rate: number;
  reason: string;
}

export interface ScorecardSpeedup {
  achieved: number;
  ideal_parallel: number;
  overhead_cost: number;
  gap: number;
  total_work_ms: number;
  critical_path_ms: number;
  virtual_makespan_multi_ms: number;
  virtual_makespan_baseline_ms: number;
  evidence_seq: number[];
}

export interface ScorecardTokens {
  multi: number;
  baseline: number;
  cost_multiplier: number;
  cost_efficiency: number;
}

export interface ScorecardPayload {
  run_id: string;
  speedup: ScorecardSpeedup;
  buckets: ScorecardBucket[];
  tokens: ScorecardTokens;
  comparability: ScorecardComparability;
  /** Present only when a chaos run scored resilience (§18.2: "25 if no chaos run"). */
  resilience_score: number | null;
  /** Present only when the run's own wall-clock makespan was persisted alongside it. */
  wall_makespan_ms: number | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function seqArray(value: unknown): number[] | null {
  if (!Array.isArray(value)) return null;
  const out: number[] = [];
  for (const v of value) {
    if (typeof v !== 'number') return null;
    out.push(v);
  }
  return out;
}

/**
 * Narrow an untyped `ScorecardResponse` into a `ScorecardPayload`, or return `null` if it
 * does not match — the panel must never render a field it cannot verify came from the
 * response (I6: every number traceable to evidence, not to an optimistic cast).
 */
export function parseScorecardPayload(raw: ScorecardResponse): ScorecardPayload | null {
  const speedupRaw = raw.speedup;
  const tokensRaw = raw.tokens;
  const comparabilityRaw = raw.comparability;
  const bucketsRaw = raw.buckets;
  if (!isRecord(speedupRaw) || !isRecord(tokensRaw) || !isRecord(comparabilityRaw)) return null;
  if (!Array.isArray(bucketsRaw)) return null;

  const achieved = num(speedupRaw.achieved);
  const ideal = num(speedupRaw.ideal_parallel);
  const overhead = num(speedupRaw.overhead_cost);
  const gap = num(speedupRaw.gap);
  const totalWork = num(speedupRaw.total_work_ms);
  const cp = num(speedupRaw.critical_path_ms);
  const vMulti = num(speedupRaw.virtual_makespan_multi_ms);
  const vBaseline = num(speedupRaw.virtual_makespan_baseline_ms);
  const speedupEvidence = seqArray(speedupRaw.evidence_seq) ?? [];
  if (
    achieved === null ||
    ideal === null ||
    overhead === null ||
    gap === null ||
    totalWork === null ||
    cp === null ||
    vMulti === null ||
    vBaseline === null
  ) {
    return null;
  }

  const buckets: ScorecardBucket[] = [];
  for (const b of bucketsRaw) {
    if (!isRecord(b)) return null;
    const bucketName = typeof b.bucket === 'string' ? b.bucket : null;
    const bcp = num(b.critical_path_ms);
    const gapContribution = num(b.gap_contribution);
    const evidence = seqArray(b.evidence_seq);
    if (bucketName === null || bcp === null || gapContribution === null || evidence === null) {
      return null;
    }
    buckets.push({
      bucket: bucketName,
      critical_path_ms: bcp,
      gap_contribution: gapContribution,
      evidence_seq: evidence,
    });
  }

  const multi = num(tokensRaw.multi);
  const baselineTokens = num(tokensRaw.baseline);
  const costMultiplier = num(tokensRaw.cost_multiplier);
  const costEfficiency = num(tokensRaw.cost_efficiency);
  if (multi === null || baselineTokens === null || costMultiplier === null || costEfficiency === null) {
    return null;
  }

  const grade = comparabilityRaw.grade;
  if (grade !== 'A' && grade !== 'B' && grade !== 'C') return null;
  const cacheReuse = num(comparabilityRaw.cache_reuse_rate);
  const cacheReuseTool = num(comparabilityRaw.cache_reuse_tool_rate);
  const cacheReuseLlm = num(comparabilityRaw.cache_reuse_llm_rate);
  const reason = typeof comparabilityRaw.reason === 'string' ? comparabilityRaw.reason : '';
  if (cacheReuse === null || cacheReuseTool === null || cacheReuseLlm === null) return null;

  const runId = typeof raw.run_id === 'string' ? raw.run_id : '';
  const resilienceRaw = raw.resilience;
  const resilienceScore = isRecord(resilienceRaw) ? num(resilienceRaw.score) : null;
  const wallMakespan = num(raw.wall_makespan_ms);

  return {
    run_id: runId,
    speedup: {
      achieved,
      ideal_parallel: ideal,
      overhead_cost: overhead,
      gap,
      total_work_ms: totalWork,
      critical_path_ms: cp,
      virtual_makespan_multi_ms: vMulti,
      virtual_makespan_baseline_ms: vBaseline,
      evidence_seq: speedupEvidence,
    },
    buckets,
    tokens: {
      multi,
      baseline: baselineTokens,
      cost_multiplier: costMultiplier,
      cost_efficiency: costEfficiency,
    },
    comparability: {
      grade,
      cache_reuse_rate: cacheReuse,
      cache_reuse_tool_rate: cacheReuseTool,
      cache_reuse_llm_rate: cacheReuseLlm,
      reason,
    },
    resilience_score: resilienceScore,
    wall_makespan_ms: wallMakespan,
  };
}

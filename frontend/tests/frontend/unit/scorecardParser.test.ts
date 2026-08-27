import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { parseScorecardPayload, type ScorecardResponse } from '../../../src/api/scorecard';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const demoChainScorecard = JSON.parse(
  readFileSync(path.join(root, 'tests/frontend/fixtures/demo_chain.scorecard.json'), 'utf8'),
) as ScorecardResponse;

describe('parseScorecardPayload (PRD §17.4, C-013 ruling)', () => {
  it('parses the real §17.4 payload computed by analysis.baseline.compare() for the week-6 demo chain fixture', () => {
    const parsed = parseScorecardPayload(demoChainScorecard);
    expect(parsed).not.toBeNull();
    // Values match `tests/analysis/test_baseline.py::
    // test_format_scorecard_prints_the_week_6_demo_milestone_block`'s asserted printed lines.
    expect(parsed?.speedup.achieved).toBeCloseTo(2.0, 9);
    expect(parsed?.speedup.ideal_parallel).toBeCloseTo(11 / 19, 9);
    expect(parsed?.speedup.total_work_ms).toBe(11);
    expect(parsed?.speedup.critical_path_ms).toBe(19);
    expect(parsed?.speedup.virtual_makespan_baseline_ms).toBe(38);
    expect(parsed?.speedup.virtual_makespan_multi_ms).toBe(19);
    expect(parsed?.comparability.grade).toBe('A');

    const handoff = parsed?.buckets.find((b) => b.bucket === 'handoff');
    const blockingWait = parsed?.buckets.find((b) => b.bucket === 'blocking_wait');
    expect(handoff?.critical_path_ms).toBe(3);
    expect(blockingWait?.critical_path_ms).toBe(5);
    for (const zeroBucket of ['retry_recovery', 'redundant_work', 'orchestration', 'unattributed']) {
      const b = parsed?.buckets.find((x) => x.bucket === zeroBucket);
      expect(b?.gap_contribution).toBe(0);
    }
  });

  it('returns null (never a partial render) when speedup is missing', () => {
    const bad = { ...demoChainScorecard } as Record<string, unknown>;
    delete bad.speedup;
    expect(parseScorecardPayload(bad as ScorecardResponse)).toBeNull();
  });

  it('returns null when a bucket is missing evidence_seq', () => {
    const bad = JSON.parse(JSON.stringify(demoChainScorecard)) as Record<string, unknown>;
    const buckets = bad.buckets as Record<string, unknown>[];
    delete buckets[0]?.evidence_seq;
    expect(parseScorecardPayload(bad as ScorecardResponse)).toBeNull();
  });

  it('returns null when comparability.grade is not A/B/C', () => {
    const bad = JSON.parse(JSON.stringify(demoChainScorecard)) as Record<string, unknown>;
    (bad.comparability as Record<string, unknown>).grade = 'Z';
    expect(parseScorecardPayload(bad as ScorecardResponse)).toBeNull();
  });

  it('treats resilience_score/wall_makespan_ms as optional — absent, not a parse failure', () => {
    const parsed = parseScorecardPayload(demoChainScorecard);
    expect(parsed).not.toBeNull();
    expect(parsed?.resilience_score).toBeNull();
    expect(parsed?.wall_makespan_ms).toBeNull();
  });
});

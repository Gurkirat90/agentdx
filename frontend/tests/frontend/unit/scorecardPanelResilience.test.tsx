import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ScorecardPayload } from '../../../src/api/scorecard';

/**
 * op2-audit-p15.md finding #4: `scorecard.resilience_score` was parsed but never rendered, and
 * `per_fault` was not even parsed. This covers the render side of the fix — the parser side is
 * covered by `scorecardParser.test.ts`'s new "resilience.per_fault" describe block.
 */

const BASE_PAYLOAD: ScorecardPayload = {
  run_id: 'r_1',
  speedup: {
    achieved: 2,
    ideal_parallel: 2.4,
    overhead_cost: -0.4,
    gap: -0.4,
    total_work_ms: 100,
    critical_path_ms: 50,
    virtual_makespan_multi_ms: 50,
    virtual_makespan_baseline_ms: 100,
    evidence_seq: [],
  },
  buckets: [],
  tokens: { multi: 10, baseline: 20, cost_multiplier: 0.5, cost_efficiency: 4 },
  comparability: {
    grade: 'A',
    cache_reuse_rate: 1,
    cache_reuse_tool_rate: 1,
    cache_reuse_llm_rate: 1,
    reason: 'identical model/tools/task',
  },
  resilience_score: null,
  per_fault: null,
  wall_makespan_ms: null,
};

let storeState: {
  scorecard: ScorecardPayload | null;
  scorecardStatus: string;
  scorecardError: string | null;
};

vi.mock('../../../src/store', () => ({
  useControlTowerStore: (selector: (s: typeof storeState) => unknown) => selector(storeState),
}));

describe('ScorecardPanel — resilience section (op2-audit-p15.md finding #4)', () => {
  it('renders nothing resilience-related when resilience_score is null (no chaos run)', async () => {
    storeState = { scorecard: BASE_PAYLOAD, scorecardStatus: 'loaded', scorecardError: null };
    const { ScorecardPanel } = await import('../../../src/panels/Scorecard');
    render(<ScorecardPanel />);
    expect(screen.queryByText('Resilience')).not.toBeInTheDocument();
  });

  it('renders the resilience score and its per-fault table together when both are present', async () => {
    storeState = {
      scorecard: {
        ...BASE_PAYLOAD,
        resilience_score: 82,
        per_fault: [
          { fault_id: 'f_00', fault_label: 'agent_crash(reviewer)', status: 'scored', score: 91, degradation_class: 'graceful' },
          { fault_id: 'f_01', fault_label: 'latency(coder->reviewer)', status: 'not_fired', score: null, degradation_class: null },
        ],
      },
      scorecardStatus: 'loaded',
      scorecardError: null,
    };
    const { ScorecardPanel } = await import('../../../src/panels/Scorecard');
    render(<ScorecardPanel />);

    expect(screen.getByText('Resilience')).toBeInTheDocument();
    expect(screen.getByText('82 / 100')).toBeInTheDocument();
    expect(screen.getByText('agent_crash(reviewer)')).toBeInTheDocument();
    expect(screen.getByText('91')).toBeInTheDocument();
    expect(screen.getByText('latency(coder->reviewer)')).toBeInTheDocument();
    // A not-fired fault's score renders as an honest "n/a", never a fabricated 0.
    expect(screen.getByText('n/a')).toBeInTheDocument();
  });
});

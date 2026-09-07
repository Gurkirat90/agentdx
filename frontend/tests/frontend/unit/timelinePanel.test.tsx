import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../src/api/client', () => ({
  api: { GET: vi.fn() },
}));

const { api } = await import('../../../src/api/client');
const { useControlTowerStore } = await import('../../../src/store');
const { TimelinePanel } = await import('../../../src/panels/Timeline');

const mockGET = api.GET as unknown as ReturnType<typeof vi.fn>;

function span(id: string, startMs: number, endMs: number) {
  return {
    bucket: null,
    end_ms: endMs,
    fault_id: null,
    kind: 'call',
    name: id,
    on_critical_path: false,
    seq_end: endMs,
    seq_start: startMs,
    span_id: id,
    start_ms: startMs,
    status: null,
  };
}

// A run whose first real event tick starts well after 0ms and whose last ends well before the
// makespan — the same shape op2-audit-p16.md finding #4 measured against real fixture data
// (support_triage: 42% short) — so Home/End jumping to the *tick* bounds instead of the true
// run bounds is observably wrong, not just theoretically.
const WATERFALL = {
  baseline_makespan_ms: null,
  lanes: [{ agent: 'coder', spans: [span('s1', 20, 80)] }],
  virtual_makespan_ms: 100,
};

beforeEach(() => {
  mockGET.mockReset();
  mockGET.mockResolvedValue({ data: { at_seq: null, at_virtual_ts: 0, keys: [] } });
  useControlTowerStore.setState({
    waterfall: WATERFALL,
    virtualTs: 50,
    playing: false,
    speed: 1,
    mode: 'replay',
    stateAt: null,
    stateAtStatus: 'idle',
    stateAtError: null,
    findings: [],
    selection: null,
    lastFired: null,
    runId: 'r_1',
  });
});

/**
 * op2-audit-p16.md finding #4: Home/End (both the `h`/`k`-adjacent keyboard shortcuts and the
 * "⏮ home" / "end ⏭" transport buttons — PRD §20.2's `[SOURCE]`-marked "home/end jump to run
 * bounds") jumped to the first/last *event tick* (a span's own start_ms/end_ms) instead of the
 * true `0`/`virtual_makespan_ms` run bounds. Measured against real fixture data, this was up to
 * 42% short of the actual run end on `support_triage`. Fixed to call `seek(0)`/`seek(makespan)`
 * directly at all four call sites.
 */
describe('TimelinePanel Home/End — true run bounds, not first/last event tick (op2-audit-p16.md finding #4)', () => {
  it('keyboard End jumps to the true makespan, not the last span\'s end_ms (80ms)', () => {
    render(<TimelinePanel />);
    fireEvent.keyDown(window, { key: 'End' });
    expect(useControlTowerStore.getState().virtualTs).toBe(100);
  });

  it('keyboard Home jumps to true 0ms, not the first span\'s start_ms (20ms)', () => {
    render(<TimelinePanel />);
    fireEvent.keyDown(window, { key: 'Home' });
    expect(useControlTowerStore.getState().virtualTs).toBe(0);
  });

  it('the "end" transport button jumps to the true makespan', () => {
    render(<TimelinePanel />);
    fireEvent.click(screen.getByRole('button', { name: 'end ⏭' }));
    expect(useControlTowerStore.getState().virtualTs).toBe(100);
  });

  it('the "home" transport button jumps to true 0ms', () => {
    render(<TimelinePanel />);
    fireEvent.click(screen.getByRole('button', { name: '⏮ home' }));
    expect(useControlTowerStore.getState().virtualTs).toBe(0);
  });
});

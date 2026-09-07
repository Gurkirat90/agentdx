import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../src/api/client', () => ({
  api: { GET: vi.fn() },
}));

// Imported after the mock so the store's `runSlice` picks up the mocked client.
const { api } = await import('../../../src/api/client');
const { useControlTowerStore } = await import('../../../src/store');

const mockGET = api.GET as unknown as ReturnType<typeof vi.fn>;

const RUN_DETAIL = { run_id: 'r_1', mode: 'multi', seed: 42, verdict: null };
const WATERFALL = { virtual_makespan_ms: 100, baseline_makespan_ms: null, lanes: [] };

beforeEach(() => {
  mockGET.mockReset();
  useControlTowerStore.setState({
    runId: null,
    run: null,
    runStatus: 'idle',
    runError: null,
    waterfall: null,
    waterfallStatus: 'idle',
    waterfallError: null,
    scorecard: null,
    scorecardStatus: 'idle',
    scorecardError: null,
  });
});

describe('runSlice.loadRun (PRD §28.4 loading order)', () => {
  it('loads run, waterfall and scorecard independently on success', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') return Promise.resolve({ data: RUN_DETAIL });
      if (urlPattern === '/api/runs/{run_id}/waterfall') return Promise.resolve({ data: WATERFALL });
      if (urlPattern === '/api/runs/{run_id}/scorecard') {
        return Promise.resolve({
          error: { error: { code: 'E-SCORE-001', message: 'not yet available' } },
          response: { status: 409 },
        });
      }
      throw new Error(`unexpected URL ${urlPattern}`);
    });

    await useControlTowerStore.getState().loadRun('r_1');
    // scorecard/waterfall promises are fired-and-forgotten (§28.4: never block each other) —
    // flush microtasks.
    await Promise.resolve();
    await Promise.resolve();

    const state = useControlTowerStore.getState();
    expect(state.runStatus).toBe('loaded');
    expect(state.run).toEqual(RUN_DETAIL);
    expect(state.waterfallStatus).toBe('loaded');
    expect(state.waterfall).toEqual(WATERFALL);
    // A 409 is the real, current, honest backend behaviour (no scorecard pipeline exists yet,
    // P17) — reported as 'unavailable', never as a generic error.
    expect(state.scorecardStatus).toBe('unavailable');
  });

  it('reports a run load failure via describeError\'s envelope-message extraction', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') {
        return Promise.resolve({
          error: { error: { code: 'E-RUN-404', message: 'Run r_missing not found' } },
        });
      }
      return Promise.resolve({ error: { error: { code: 'E', message: 'n/a' } }, response: { status: 500 } });
    });

    await useControlTowerStore.getState().loadRun('r_missing');

    const state = useControlTowerStore.getState();
    expect(state.runStatus).toBe('error');
    expect(state.runError).toBe('Run r_missing not found');
  });

  it('a superseded loadRun call never clobbers the newer run\'s state', async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      return Promise.resolve({ data: WATERFALL });
    });

    const firstLoad = useControlTowerStore.getState().loadRun('r_old');
    // Second call starts before the first resolves.
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') return Promise.resolve({ data: { ...RUN_DETAIL, run_id: 'r_new' } });
      return Promise.resolve({ data: WATERFALL });
    });
    await useControlTowerStore.getState().loadRun('r_new');

    resolveFirst({ data: { ...RUN_DETAIL, run_id: 'r_old' } });
    await firstLoad;

    expect(useControlTowerStore.getState().runId).toBe('r_new');
    expect(useControlTowerStore.getState().run?.run_id).toBe('r_new');
  });
});

describe('runSlice.loadRun — unguarded openapi-fetch result (op2-audit-p15.md finding #2)', () => {
  // `openapi-fetch` can resolve `{data: undefined, error: undefined}` on a non-JSON error body
  // (this build's Vite dev-proxy 500 page when the backend is unreachable) — the same shape
  // that broke graphSlice/findingsSlice/chaosSlice before D-60's fix. Before this repair,
  // runSlice trusted `!result.error` alone, so `waterfall`/`scorecard` state silently became
  // `undefined` instead of surfacing an error.

  it('treats a waterfall result with data===undefined and no error as a load failure, not a silent undefined', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') return Promise.resolve({ data: RUN_DETAIL });
      if (urlPattern === '/api/runs/{run_id}/waterfall') {
        return Promise.resolve({ data: undefined, error: undefined });
      }
      // The real openapi-fetch client always includes `response` (the underlying fetch
      // Response), even on this failure shape — a mock omitting it here would exercise a
      // combination that cannot occur in production and would mask the assertion below behind
      // an unrelated TypeError.
      return Promise.resolve({ data: undefined, error: undefined, response: { status: 502 } });
    });

    await useControlTowerStore.getState().loadRun('r_1');
    await Promise.resolve();
    await Promise.resolve();

    const state = useControlTowerStore.getState();
    expect(state.waterfallStatus).toBe('error');
    expect(state.waterfall).toBeNull();
    expect(state.waterfallError).toBeTruthy();
  });

  it('treats a scorecard result with data===undefined and no error as a load failure, not a silent undefined', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') return Promise.resolve({ data: RUN_DETAIL });
      if (urlPattern === '/api/runs/{run_id}/waterfall') return Promise.resolve({ data: WATERFALL });
      if (urlPattern === '/api/runs/{run_id}/scorecard') {
        return Promise.resolve({ data: undefined, error: undefined, response: { status: 200 } });
      }
      throw new Error(`unexpected URL ${urlPattern}`);
    });

    await useControlTowerStore.getState().loadRun('r_1');
    await Promise.resolve();
    await Promise.resolve();

    const state = useControlTowerStore.getState();
    expect(state.scorecardStatus).toBe('error');
    expect(state.scorecard).toBeNull();
    expect(state.scorecardError).toBeTruthy();
  });

  it('catches a parseScorecardPayload throw instead of leaving scorecardStatus stuck at loading forever', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') return Promise.resolve({ data: RUN_DETAIL });
      if (urlPattern === '/api/runs/{run_id}/waterfall') return Promise.resolve({ data: WATERFALL });
      if (urlPattern === '/api/runs/{run_id}/scorecard') {
        // `data: null` passes the `data === undefined` guard (it is not `undefined`) but
        // `parseScorecardPayload(null)` throws on its first line (`raw.speedup`) — exactly the
        // unhandled-rejection path finding #2 describes for any payload shape narrower than
        // `undefined` alone.
        return Promise.resolve({ data: null, response: { status: 200 } });
      }
      throw new Error(`unexpected URL ${urlPattern}`);
    });

    await useControlTowerStore.getState().loadRun('r_1');
    await Promise.resolve();
    await Promise.resolve();

    const state = useControlTowerStore.getState();
    expect(state.scorecardStatus).toBe('error');
    expect(state.scorecardError).toContain('could not be parsed');
  });
});

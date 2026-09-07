import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../src/api/client', () => ({
  api: { GET: vi.fn() },
}));

// Imported after the mock, same pattern as runSlice.test.ts, so the store's `timelineSlice`
// picks up the mocked client.
const { api } = await import('../../../src/api/client');
const { useControlTowerStore } = await import('../../../src/store');

const mockGET = api.GET as unknown as ReturnType<typeof vi.fn>;

const STATE_AT: unknown = { at_seq: 4, at_virtual_ts: 120, keys: [] };

beforeEach(() => {
  mockGET.mockReset();
  useControlTowerStore.setState({
    virtualTs: null,
    playing: false,
    speed: 1,
    mode: 'replay',
    stateAt: null,
    stateAtStatus: 'idle',
    stateAtError: null,
  });
});

/**
 * `timelineSlice.loadStateAt` half of op2-audit-p16.md finding #1 (the `eventsSlice.ts` half
 * is covered by `eventsSlice.test.ts`'s own "degrade-to-polling" describe block). Before the
 * fix, `if (result.error)` alone let a response shaped `{ data: undefined, error: undefined }`
 * (this codebase's documented `openapi-fetch` failure mode — D-60 — triggered by a non-JSON
 * error body, e.g. a dev-proxy 500 page) fall through to `set({ stateAt: undefined, ... })`
 * while marking `stateAtStatus: 'loaded'`. `Timeline.tsx` only guards `stateAt === null`
 * (`undefined !== null`), so it would then crash reading `stateAt.at_virtual_ts` instead of
 * showing the intended error state.
 */
describe('timelineSlice.loadStateAt (op2-audit-p16.md finding #1)', () => {
  it('loads state successfully on a well-formed response', async () => {
    mockGET.mockResolvedValue({ data: STATE_AT });
    useControlTowerStore.getState().setVirtualTs(120);

    await useControlTowerStore.getState().loadStateAt('r_1', 120);

    const state = useControlTowerStore.getState();
    expect(state.stateAtStatus).toBe('loaded');
    expect(state.stateAt).toEqual(STATE_AT);
    expect(state.stateAtError).toBeNull();
  });

  it('treats the documented data-undefined/error-undefined shape as an error, never as loaded with undefined data', async () => {
    mockGET.mockResolvedValue({ data: undefined, error: undefined, response: { status: 502 } });
    useControlTowerStore.getState().setVirtualTs(120);

    await useControlTowerStore.getState().loadStateAt('r_1', 120);

    const state = useControlTowerStore.getState();
    // The regression this guards against: stateAtStatus === 'loaded' with stateAt still
    // undefined, which Timeline.tsx's `stateAt === null` check does not catch.
    expect(state.stateAtStatus).toBe('error');
    expect(state.stateAt).toBeNull();
    expect(state.stateAtError).toBe('State reconstruction failed.');
  });

  it('still reports an error on the ordinary { error } response shape', async () => {
    mockGET.mockResolvedValue({
      error: { error: { code: 'E-STATE-001', message: 'bad request' } },
      response: { status: 400 },
    });
    useControlTowerStore.getState().setVirtualTs(120);

    await useControlTowerStore.getState().loadStateAt('r_1', 120);

    expect(useControlTowerStore.getState().stateAtStatus).toBe('error');
  });

  it('a stale in-flight response never clobbers a newer scrub position\'s result', async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    mockGET.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
    );
    useControlTowerStore.getState().setVirtualTs(100);
    const firstCall = useControlTowerStore.getState().loadStateAt('r_1', 100);

    // The scrub moves on before the first request resolves.
    mockGET.mockResolvedValue({ data: STATE_AT });
    useControlTowerStore.getState().setVirtualTs(120);
    await useControlTowerStore.getState().loadStateAt('r_1', 120);

    expect(useControlTowerStore.getState().stateAt).toEqual(STATE_AT);

    // Now the stale first call finally resolves — it must not overwrite the newer result.
    resolveFirst({ data: { at_seq: 1, at_virtual_ts: 100, keys: [] } });
    await firstCall;

    expect(useControlTowerStore.getState().stateAt).toEqual(STATE_AT);
  });
});

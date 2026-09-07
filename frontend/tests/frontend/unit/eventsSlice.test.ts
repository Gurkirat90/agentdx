import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../../src/api/client', () => ({
  api: { GET: vi.fn() },
}));

// Imported after the mock, same pattern as runSlice.test.ts, so eventsSlice/runSlice pick up
// the mocked client.
const { api } = await import('../../../src/api/client');
const { useControlTowerStore } = await import('../../../src/store');

const mockGET = api.GET as unknown as ReturnType<typeof vi.fn>;

/**
 * OP-3 repair (2026-08-24, following an independent post-build review). `eventsSlice.ts` had
 * no unit tests at all — only indirect e2e coverage. Three real gaps this file closes:
 *
 * 1. The PRD §26.2 heartbeat (`ping` every ~15s) was simply missing — nothing sent one. Fixed
 *    in `eventsSlice.ts`; proven here against a fake-timer clock.
 * 2. A terminal `status` frame (`complete`/`failed`/`aborted_guard`) is now supposed to
 *    re-fetch `run` via `loadRun` (PRD §26.2/§28.4 step 3) — previously written to
 *    `runStatus_ws` and never read anywhere. Proven here directly against the store.
 * 3. `wsReconnect.spec.ts` (e2e) narrates "no re-delivery of 1-3" but its mock server never
 *    actually resends a duplicate, so the client's own dedup guard (`applyEvent`'s
 *    `seq <= maxSeqSeen` check) was never exercised by any test. Proven here by actually
 *    resending a duplicate seq and asserting it was dropped, not double-applied.
 *
 * A minimal in-memory `WebSocket` stand-in — this project's e2e suite already mocks the
 * socket at the network layer (Playwright's `page.routeWebSocket`); this does the same thing
 * at the unit level so the dedup/heartbeat/status-refetch logic can be asserted directly
 * against store state, in milliseconds, with no browser.
 */
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  // --- test-only driver methods, not part of the real WebSocket surface ---
  simulateOpen(): void {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  simulateMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  sentFramesOfType(type: string): Record<string, unknown>[] {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>).filter((f) => f.type === type);
  }
}

vi.stubGlobal('WebSocket', MockWebSocket);

function makeEvent(seq: number): Record<string, unknown> {
  return {
    schema_version: 1,
    run_id: 'r_1',
    seq,
    sched_step: seq,
    virtual_ts_ms: seq * 10,
    wall_ts_ms: seq * 10,
    vclock: {},
    type: 'span_start',
    causal_parents: [],
    payload: {},
    agent_id: 'coder',
    clock_slot: null,
    span_id: null,
    fault_id: null,
  };
}

beforeEach(() => {
  MockWebSocket.instances = [];
  mockGET.mockReset();
  useControlTowerStore.setState({
    wsStatus: 'idle',
    wsSamplingN: null,
    wsReconnectAttempt: 0,
    liveEvents: [],
    maxSeqSeen: -1,
    runStatus_ws: null,
    activeFaultAgents: {},
    runId: null,
    run: null,
    runStatus: 'idle',
  });
});

afterEach(() => {
  // Tears down the module-level socket/timer runtime so it doesn't leak into the next test.
  useControlTowerStore.getState().disconnectWs();
  vi.useRealTimers();
});

describe('eventsSlice dedup (PRD §26.2: "no event is ever delivered twice")', () => {
  it('drops a genuinely re-delivered event instead of double-applying it', () => {
    useControlTowerStore.getState().connectWs('r_1');
    const ws = MockWebSocket.instances.at(-1)!;
    ws.simulateOpen();
    ws.simulateMessage({ type: 'hello', run_id: 'r_1' });
    ws.simulateMessage({ type: 'events', events: [makeEvent(1), makeEvent(2), makeEvent(3)] });

    expect(useControlTowerStore.getState().liveEvents).toHaveLength(3);
    expect(useControlTowerStore.getState().maxSeqSeen).toBe(3);

    // The exact reconnect race this module's own comment says it defends against: seq 3
    // arrives a second time, alongside a genuinely new seq 4.
    ws.simulateMessage({ type: 'events', events: [makeEvent(3), makeEvent(4)] });

    const state = useControlTowerStore.getState();
    expect(state.liveEvents).toHaveLength(4); // not 5 — the duplicate seq 3 was dropped
    expect(state.liveEvents.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
    expect(state.maxSeqSeen).toBe(4);
  });
});

describe('eventsSlice heartbeat (PRD §26.2: "ping/pong every 15s")', () => {
  it('sends a ping roughly every 15s while the socket is open', () => {
    vi.useFakeTimers();
    useControlTowerStore.getState().connectWs('r_1');
    const ws = MockWebSocket.instances.at(-1)!;
    ws.simulateOpen();

    expect(ws.sentFramesOfType('subscribe')).toHaveLength(1);
    expect(ws.sentFramesOfType('ping')).toHaveLength(0);

    vi.advanceTimersByTime(15000);
    expect(ws.sentFramesOfType('ping')).toHaveLength(1);

    vi.advanceTimersByTime(15000);
    expect(ws.sentFramesOfType('ping')).toHaveLength(2);
  });

  it('stops sending pings once the socket closes', () => {
    vi.useFakeTimers();
    useControlTowerStore.getState().connectWs('r_1');
    const ws = MockWebSocket.instances.at(-1)!;
    ws.simulateOpen();
    vi.advanceTimersByTime(15000);
    expect(ws.sentFramesOfType('ping')).toHaveLength(1);

    useControlTowerStore.getState().disconnectWs();
    const pingsAtClose = ws.sentFramesOfType('ping').length;
    vi.advanceTimersByTime(45000);
    expect(ws.sentFramesOfType('ping')).toHaveLength(pingsAtClose); // no more sent after teardown
  });
});

describe('eventsSlice terminal status → live run re-fetch (PRD §26.2/§28.4 step 3)', () => {
  it('re-fetches the run via loadRun when a terminal status frame arrives', async () => {
    mockGET.mockImplementation((urlPattern: string) => {
      if (urlPattern === '/api/runs/{run_id}') {
        return Promise.resolve({ data: { run_id: 'r_1', status: 'complete' } });
      }
      if (urlPattern === '/api/runs/{run_id}/waterfall') {
        return Promise.resolve({ data: { virtual_makespan_ms: 1, baseline_makespan_ms: null, lanes: [] } });
      }
      if (urlPattern === '/api/runs/{run_id}/scorecard') {
        return Promise.resolve({
          error: { error: { code: 'E-SCORE-001', message: 'not yet available' } },
          response: { status: 409 },
        });
      }
      throw new Error(`unexpected URL ${urlPattern}`);
    });

    useControlTowerStore.getState().connectWs('r_1');
    const ws = MockWebSocket.instances.at(-1)!;
    ws.simulateOpen();
    expect(mockGET).not.toHaveBeenCalled(); // nothing fetched yet — only a WS subscribe so far

    ws.simulateMessage({ type: 'status', status: 'complete' });
    // loadRun's own promise chain — flush microtasks.
    await Promise.resolve();
    await Promise.resolve();

    expect(mockGET).toHaveBeenCalledWith('/api/runs/{run_id}', { params: { path: { run_id: 'r_1' } } });
    expect(useControlTowerStore.getState().run).toEqual({ run_id: 'r_1', status: 'complete' });
    expect(useControlTowerStore.getState().runStatus_ws).toBe('complete');
  });

  it('does not re-fetch on a non-terminal status frame', () => {
    useControlTowerStore.getState().connectWs('r_1');
    const ws = MockWebSocket.instances.at(-1)!;
    ws.simulateOpen();

    ws.simulateMessage({ type: 'status', status: 'running' });

    expect(mockGET).not.toHaveBeenCalled();
    expect(useControlTowerStore.getState().runStatus_ws).toBe('running');
  });
});

describe('eventsSlice degrade-to-polling — unguarded openapi-fetch result (op2-audit-p16.md finding #1)', () => {
  /** Drives the module through PRD §26.2's degrade-to-polling path. `scheduleReconnect` reads
   * `wsReconnectAttempt` *before* incrementing it, so it takes MAX_RECONNECT_ATTEMPTS + 1 (7)
   * closes to reach the ceiling: the first 6 each schedule one more reconnect (raising the
   * counter to 6), and the 7th finds `attempt(6) >= MAX(6)` already true and calls
   * `startPolling` synchronously instead of opening another socket. 10s per reconnect step
   * safely covers the real backoff's worst case (8000ms base * 1.2 jitter = 9600ms); the final
   * close needs no timer advance since `startPolling` runs synchronously inside it. */
  async function driveToPolling(): Promise<void> {
    useControlTowerStore.getState().connectWs('r_1');
    MockWebSocket.instances.at(-1)!.simulateOpen();
    for (let i = 0; i < 6; i++) {
      MockWebSocket.instances.at(-1)!.close();
      await vi.advanceTimersByTimeAsync(10000);
    }
    MockWebSocket.instances.at(-1)!.close();
    expect(useControlTowerStore.getState().wsStatus).toBe('polling');
  }

  it('a malformed poll response (data undefined, no error — the documented dev-proxy-500 shape) does not become an unhandled promise rejection', async () => {
    vi.useFakeTimers();
    const rejections: unknown[] = [];
    const onRejection = (reason: unknown) => rejections.push(reason);
    process.on('unhandledRejection', onRejection);
    try {
      mockGET.mockImplementation(() => Promise.resolve({ data: undefined, error: undefined }));
      await driveToPolling();

      // One poll tick: before this fix, `result.data.events` threw inside the bare
      // `void (async () => {...})()` with no `.catch()`, an unhandled rejection every
      // POLL_INTERVAL_MS for as long as the malformed response recurred.
      await vi.advanceTimersByTimeAsync(2000);

      expect(rejections).toHaveLength(0);
      expect(useControlTowerStore.getState().liveEvents).toHaveLength(0);
    } finally {
      process.off('unhandledRejection', onRejection);
    }
  });

  it('a well-formed poll response still applies its events normally once polling', async () => {
    vi.useFakeTimers();
    mockGET.mockImplementation(() =>
      Promise.resolve({ data: { events: [makeEvent(1), makeEvent(2)] } }),
    );
    await driveToPolling();

    await vi.advanceTimersByTimeAsync(2000);

    const state = useControlTowerStore.getState();
    expect(state.liveEvents).toHaveLength(2);
    expect(state.maxSeqSeen).toBe(2);
  });
});

/**
 * `eventsSlice` (P16) — the live WebSocket stream (PRD §26.2, §28.4 step 3). Not one of P15's
 * four named slices; the fuller PRD `eventsSlice` (`store/README.md`: "event pages,
 * `maxSeqLoaded`, WS connection state") is explicitly P16 scope.
 *
 * Design constraint 6 ("reconnect without duplication or gaps, and degrade to polling with a
 * visible state, never a silently stale view"):
 *  - **No duplicates, no gaps**: every (re)connect subscribes with `from_seq = maxSeqSeen + 1`
 *    (PRD §26.2's own reconnect contract — "no event is ever delivered twice"). Seq is a
 *    monotonic, gap-free counter per run (PRD §9), so resuming from the next expected seq is
 *    sufficient; this module additionally asserts monotonicity defensively and drops (never
 *    re-applies) a seq at or below what has already been seen, in case a reconnect races a
 *    stale in-flight frame.
 *  - **Reconnect**: exponential backoff (500ms -> 8s cap, plus jitter) up to
 *    `MAX_RECONNECT_ATTEMPTS`; `wsStatus` is `'reconnecting'` for the whole backoff window, a
 *    real, visible state — never silently presented as `'open'`.
 *  - **Degrade to polling**: once reconnection attempts are exhausted, `wsStatus` becomes
 *    `'polling'` and a `setInterval` re-fetches `GET /api/runs/{id}/events?from_seq=` on
 *    `POLL_INTERVAL_MS` — the same real REST endpoint PRD §26.1 already defines, so "degraded"
 *    still means real, un-stale data, just not pushed. The badge this drives is
 *    `wsStatus`/`wsSamplingN` themselves, read directly by any panel that wants to show it
 *    (Graph's live-pulse gating, a connection badge in `RunRoute`'s top bar).
 *  - **Sampling** (PRD §26.2 backpressure): the server's `{"type":"status","sampling":N}`
 *    frame is recorded as `wsSamplingN` verbatim — this client never infers or estimates it.
 *
 * **OP-3 repair (2026-08-24, following an independent post-build review):** two PRD §26.2
 * requirements were received/receivable but not actually wired through: (1) a client-side
 * `ping` heartbeat, every `HEARTBEAT_INTERVAL_MS` — without it, a genuinely quiet-but-healthy
 * run (no server-pushed event for >45s) tripped the server's own silence timeout and forced an
 * unnecessary reconnect; (2) a terminal `status` frame (`complete`/`failed`/`aborted_guard`)
 * now re-fetches `run` via the store's existing `loadRun`, so the header/verdict/scorecard
 * update live when a run finishes mid-session (PRD §28.4 step 3), instead of staying frozen at
 * whatever they were on page load until a manual reload.
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import type { FindingsSlice } from './findingsSlice';
import type { RunSlice } from './runSlice';
import type { LiveEvent } from './types';

export type WsStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'polling' | 'closed';

const MAX_LIVE_EVENTS = 5000;
const BASE_RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 8000;
const MAX_RECONNECT_ATTEMPTS = 6;
const POLL_INTERVAL_MS = 2000;
// OP-3 repair (2026-08-24, post-OP-2): PRD §26.2's own protocol table — "Heartbeat: ping/pong
// every 15s; server closes after 45s of silence." `api/ws.py`'s server-side timeout
// (`config.ws_heartbeat_timeout_s`, default 45s) treats *any* client message as activity, but
// until this repair the client's only outbound traffic was a reactive `ack` sent in response
// to an incoming event — a genuinely quiet-but-healthy run (a slow LLM call, an agent turn
// with no new events for >45s) produced no client traffic at all and was closed by the server
// as if stale, triggering an unnecessary reconnect. This interval is a real, protocol-named
// heartbeat, not inferred from event traffic.
const HEARTBEAT_INTERVAL_MS = 15000;

function wsBaseUrl(): string {
  if (typeof window === 'undefined') return 'ws://127.0.0.1:8420';
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}`;
}

export interface EventsSlice {
  wsStatus: WsStatus;
  wsSamplingN: number | null;
  wsReconnectAttempt: number;
  liveEvents: LiveEvent[];
  maxSeqSeen: number;
  runStatus_ws: string | null;
  /** Agent ids currently under an observed live fault (Graph panel's "ring", PRD §20.5). A
   * simplification, declared: cleared on a fixed real-time timeout rather than a genuine
   * fault-resolution signal, because no event type in this build's schema marks "this fault
   * is over" for the transport-class faults this run's WS stream can observe live (only
   * `agent_crash`, wired end to end per CONTEXT.md §5 row 9, produces an observable
   * `fault_injected` on this build's real call sites). */
  activeFaultAgents: Record<string, string>;

  connectWs: (runId: string) => void;
  disconnectWs: () => void;
}

interface _WsRuntime {
  socket: WebSocket | null;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  pollTimer: ReturnType<typeof setInterval> | null;
  /** PRD §26.2 heartbeat (OP-3 repair) — started on `onopen`, stopped whenever the socket
   * closes or is torn down, so it never outlives the connection it belongs to. */
  heartbeatTimer: ReturnType<typeof setInterval> | null;
  faultRingTimers: Map<string, ReturnType<typeof setTimeout>>;
  currentRunId: string | null;
  /** Set by `disconnectWs` so an in-flight reconnect/poll callback never resurrects a
   * connection the caller explicitly tore down (e.g. navigating away, or a completed run). */
  torn: boolean;
}

// Module-level, not store state: sockets/timers are not serialisable UI state and must never
// be read by a selector — `wsStatus` etc. above are the only externally-observable surface.
const runtime: _WsRuntime = {
  socket: null,
  reconnectTimer: null,
  pollTimer: null,
  heartbeatTimer: null,
  faultRingTimers: new Map(),
  currentRunId: null,
  torn: false,
};

function clearTimers(): void {
  if (runtime.reconnectTimer !== null) clearTimeout(runtime.reconnectTimer);
  if (runtime.pollTimer !== null) clearInterval(runtime.pollTimer);
  if (runtime.heartbeatTimer !== null) clearInterval(runtime.heartbeatTimer);
  runtime.reconnectTimer = null;
  runtime.pollTimer = null;
  runtime.heartbeatTimer = null;
}

export const createEventsSlice: StateCreator<
  EventsSlice & Pick<FindingsSlice, 'findings'> & Pick<RunSlice, 'loadRun'>,
  [],
  [],
  EventsSlice
> = (set, get) => {
  function applyEvent(event: LiveEvent): void {
    const state = get();
    if (event.seq <= state.maxSeqSeen) return; // already applied — reconnect-safe, no dup
    set((s) => ({
      liveEvents: [...s.liveEvents, event].slice(-MAX_LIVE_EVENTS),
      maxSeqSeen: event.seq,
    }));
    if (event.type === 'fault_injected') {
      const target = event.payload.target;
      if (typeof target === 'string' && typeof event.fault_id === 'string') {
        const faultId = event.fault_id;
        set((s) => ({ activeFaultAgents: { ...s.activeFaultAgents, [target]: faultId } }));
        const prior = runtime.faultRingTimers.get(target);
        if (prior !== undefined) clearTimeout(prior);
        runtime.faultRingTimers.set(
          target,
          setTimeout(() => {
            set((s) => {
              const next = { ...s.activeFaultAgents };
              delete next[target];
              return { activeFaultAgents: next };
            });
          }, 8000),
        );
      }
    }
  }

  function scheduleReconnect(runId: string): void {
    if (runtime.torn) return;
    const attempt = get().wsReconnectAttempt;
    if (attempt >= MAX_RECONNECT_ATTEMPTS) {
      startPolling(runId);
      return;
    }
    set({ wsStatus: 'reconnecting', wsReconnectAttempt: attempt + 1 });
    const delay = Math.min(BASE_RECONNECT_DELAY_MS * 2 ** attempt, MAX_RECONNECT_DELAY_MS);
    const jitter = delay * (0.8 + Math.random() * 0.4);
    runtime.reconnectTimer = setTimeout(() => openSocket(runId), jitter);
  }

  function startPolling(runId: string): void {
    if (runtime.torn) return;
    set({ wsStatus: 'polling' });
    clearTimers();
    runtime.pollTimer = setInterval(() => {
      void (async () => {
        if (runtime.torn) return;
        const fromSeq = get().maxSeqSeen + 1;
        const result = await api.GET('/api/runs/{run_id}/events', {
          params: { path: { run_id: runId }, query: { from_seq: fromSeq, limit: 1000 } },
        });
        if (runtime.torn) return;
        // `result.error` alone is not a reliable success guard: `openapi-fetch` can come back
        // with neither `error` nor `data` set (a non-JSON error body, e.g. a dev-proxy 500 page
        // when the backend is unreachable). Before this fix, that shape reached
        // `result.data.events` unguarded — a `TypeError` thrown inside this bare `void
        // (async () => {...})()`, with no `.catch()`, inside a `setInterval` callback: an
        // unhandled promise rejection outside React's render cycle, invisible to
        // `PanelErrorBoundary` (which only catches render-time exceptions), repeating silently
        // every `POLL_INTERVAL_MS` for as long as the malformed response kept recurring
        // (op2-audit-p16.md finding #1; same defect class D-60 already fixed elsewhere).
        if (result.error || result.data === undefined) return;
        for (const event of result.data.events) applyEvent(event as unknown as LiveEvent);
      })();
    }, POLL_INTERVAL_MS);
  }

  function openSocket(runId: string): void {
    if (runtime.torn) return;
    set({ wsStatus: 'connecting' });
    const socket = new WebSocket(`${wsBaseUrl()}/ws/runs/${runId}`);
    runtime.socket = socket;

    socket.onopen = () => {
      if (runtime.torn) return;
      const fromSeq = get().maxSeqSeen + 1;
      socket.send(JSON.stringify({ type: 'subscribe', from_seq: fromSeq }));
      // PRD §26.2 heartbeat (OP-3 repair): send real client traffic on our own clock instead
      // of only reacting to server pushes, so a quiet-but-healthy run never looks stale to the
      // server's 45s silence timeout — see HEARTBEAT_INTERVAL_MS's comment above.
      if (runtime.heartbeatTimer !== null) clearInterval(runtime.heartbeatTimer);
      runtime.heartbeatTimer = setInterval(() => {
        if (runtime.torn || socket.readyState !== WebSocket.OPEN) return;
        socket.send(JSON.stringify({ type: 'ping' }));
      }, HEARTBEAT_INTERVAL_MS);
    };
    socket.onmessage = (raw) => {
      if (runtime.torn) return;
      let msg: unknown;
      try {
        msg = JSON.parse(String(raw.data));
      } catch {
        return;
      }
      if (typeof msg !== 'object' || msg === null || !('type' in msg)) return;
      const m = msg as Record<string, unknown>;
      switch (m.type) {
        case 'hello':
          set({ wsStatus: 'open', wsReconnectAttempt: 0 });
          break;
        case 'events':
          if (Array.isArray(m.events)) {
            for (const e of m.events) applyEvent(e as LiveEvent);
          }
          break;
        case 'event':
          if (m.event) applyEvent(m.event as LiveEvent);
          break;
        case 'finding':
          if (m.finding && typeof m.finding === 'object') {
            const finding = m.finding as FindingsSlice['findings'][number];
            set((s) => (s.findings.some((f) => f.id === finding.id) ? s : { findings: [...s.findings, finding] }));
          }
          break;
        case 'status':
          if (typeof m.status === 'string') {
            set({ runStatus_ws: m.status });
            // OP-3 repair (2026-08-24, post-OP-2): PRD §26.2's status frame / §28.4 step 3
            // ("re-evaluated whenever run.status changes — a run this page opened while
            // running can finish mid-session") named this exact behaviour, but until this
            // repair `runStatus_ws` was written and never read anywhere. Re-fetching through
            // the same `loadRun` the initial page load already uses is deliberate, not just
            // convenient: completion is precisely when the verdict and scorecard (previously
            // `null`/409-unavailable) first become real, so a full `run` re-fetch is what a
            // reload would have shown anyway — not a narrower "just flip the status" patch.
            // `RunRoute`'s existing `run?.status !== 'running'` effect then disconnects this
            // socket once `run.status` reflects the change; no separate teardown call needed
            // here, and no new endpoint was added.
            if (m.status === 'complete' || m.status === 'failed' || m.status === 'aborted_guard') {
              void get().loadRun(runId);
            }
          }
          if (typeof m.sampling === 'number') set({ wsSamplingN: m.sampling });
          break;
        default:
          break; // verdict/pong/error frames: no P16 panel needs them beyond the connection state
      }
      // ack immediately (PRD §26.2 flow control) — this client applies events synchronously,
      // so there is never a real backlog to hold acks back for.
      if (typeof m.type === 'string' && (m.type === 'event' || m.type === 'events')) {
        socket.send(JSON.stringify({ type: 'ack', through_seq: get().maxSeqSeen }));
      }
    };
    socket.onclose = () => {
      if (runtime.heartbeatTimer !== null) clearInterval(runtime.heartbeatTimer);
      runtime.heartbeatTimer = null;
      runtime.socket = null;
      if (runtime.torn) return;
      scheduleReconnect(runId);
    };
    socket.onerror = () => {
      socket.close();
    };
  }

  return {
    wsStatus: 'idle',
    wsSamplingN: null,
    wsReconnectAttempt: 0,
    liveEvents: [],
    maxSeqSeen: -1,
    runStatus_ws: null,
    activeFaultAgents: {},

    connectWs: (runId) => {
      runtime.torn = false;
      if (runtime.currentRunId !== runId) {
        // A genuinely new run: reset the cursor rather than resuming a different run's seq
        // space — reconnect-safety only applies within one run's own connection lifetime.
        runtime.currentRunId = runId;
        set({ liveEvents: [], maxSeqSeen: -1, wsReconnectAttempt: 0, activeFaultAgents: {} });
      }
      clearTimers();
      openSocket(runId);
    },

    disconnectWs: () => {
      runtime.torn = true;
      clearTimers();
      for (const t of runtime.faultRingTimers.values()) clearTimeout(t);
      runtime.faultRingTimers.clear();
      if (runtime.socket !== null) {
        runtime.socket.onclose = null; // this is a deliberate close — do not reconnect
        runtime.socket.close();
        runtime.socket = null;
      }
      runtime.currentRunId = null;
      set({ wsStatus: 'closed' });
    },
  };
};

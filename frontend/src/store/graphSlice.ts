/**
 * `graphSlice` (P16) — `GET /api/runs/{id}/graph` (PRD §26.1, §20.5). Not one of P15's four
 * named slices (`run`/`selection`/`timeline`/`findings`); P16's own DELIVERABLES ("store: …
 * extended") is where this is added, following the same "one slice per REST resource" shape
 * `runSlice`/`findingsSlice` already established rather than folding graph data into either.
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import type { FetchStatus, GraphResponse } from './types';

export interface GraphSlice {
  graph: GraphResponse | null;
  graphStatus: FetchStatus;
  graphError: string | null;
  /** OP-3 repair (2026-08-25): which `runId` `graph`/`graphStatus` currently describe. Not
   * merely "which run's fetch is in flight" — `GraphPanel` compares this against its own
   * `runId` prop on every render, synchronously, so a freshly-mounted panel (post
   * `key={runId}` remount, `RunRoute.tsx`) never reads the *previous* run's leftover `graph`
   * object on its first render, before this slice's own `loadGraph` effect has had a chance
   * to reset it. Resetting inside a `useEffect` alone (the pre-repair state) is one render
   * too late: React commits the new `GraphPanel` instance — and runs its first render —
   * before any effect fires, so a crash-shaped leftover (D-60's own bug class) reproduces on
   * the fresh instance and gets caught by the fresh boundary, looking identical to the
   * original "boundary never resets" bug it was mistaken for the first time around. See
   * `panelErrorBoundary.spec.ts`'s docstring for the full account. */
  graphRunId: string | null;
  loadGraph: (runId: string) => Promise<void>;
}

export const createGraphSlice: StateCreator<GraphSlice, [], [], GraphSlice> = (set, get) => ({
  graph: null,
  graphStatus: 'idle',
  graphError: null,
  graphRunId: null,

  loadGraph: async (runId) => {
    set({ graphStatus: 'loading', graphError: null, graphRunId: runId });
    const result = await api.GET('/api/runs/{run_id}/graph', {
      params: { path: { run_id: runId } },
    });
    if (get().graphRunId !== runId) return; // superseded by a newer loadGraph call
    // `result.error` alone is not a reliable success guard: `openapi-fetch` can come back
    // with neither `error` nor `data` set (e.g. a non-JSON error body — a dev-proxy 500 page
    // when the backend is unreachable — fails the client's JSON parse silently rather than
    // populating `error`). Trusting `!result.error` alone here previously let `graph` become
    // `undefined` while `graphStatus` was set to `'loaded'`, which crashed `GraphPanel`'s own
    // `graph === null` guard (`undefined !== null`) instead of showing its error state.
    if (result.error || result.data === undefined) {
      set({ graphStatus: 'error', graphError: 'Request failed.' });
      return;
    }
    set({ graph: result.data, graphStatus: 'loaded' });
  },
});

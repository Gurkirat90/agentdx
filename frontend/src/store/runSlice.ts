/**
 * `runSlice` (PRD §28.2): "Run metadata, verdict, scorecard, load status." Fetched once per
 * run. Scoped narrowing for this prompt (DELIVERABLES names only `run, selection, timeline,
 * findings`, not the fuller PRD `eventsSlice`/`chaosSlice`/`prefsSlice`): since data loading
 * here is REST-only, no live event table, this slice also holds the waterfall response
 * directly — it is exactly the data one `/runs/{id}` + `/runs/{id}/waterfall` +
 * `/runs/{id}/scorecard` page needs, and nothing an eventsSlice would otherwise own.
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import { parseScorecardPayload, type ScorecardPayload } from '../api/scorecard';
import type { FetchStatus, RunDetail, ScorecardStatus, WaterfallResponse } from './types';

export interface RunSlice {
  runId: string | null;
  run: RunDetail | null;
  runStatus: FetchStatus;
  runError: string | null;

  waterfall: WaterfallResponse | null;
  waterfallStatus: FetchStatus;
  waterfallError: string | null;

  scorecard: ScorecardPayload | null;
  scorecardStatus: ScorecardStatus;
  scorecardError: string | null;

  loadRun: (runId: string) => Promise<void>;
}

export const createRunSlice: StateCreator<RunSlice, [], [], RunSlice> = (set, get) => ({
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

  /**
   * Loading order matches PRD §28.4: the verdict pill (part of `run`) must not wait behind
   * the waterfall or scorecard, so `run` is awaited first and the other two are fired in
   * parallel and settle independently — a slow scorecard never blocks the waterfall from
   * appearing, or vice versa.
   */
  loadRun: async (runId) => {
    set({
      runId,
      runStatus: 'loading',
      runError: null,
      waterfall: null,
      waterfallStatus: 'loading',
      waterfallError: null,
      scorecard: null,
      scorecardStatus: 'loading',
      scorecardError: null,
    });

    const runPromise = api.GET('/api/runs/{run_id}', { params: { path: { run_id: runId } } });
    const waterfallPromise = api.GET('/api/runs/{run_id}/waterfall', {
      params: { path: { run_id: runId } },
    });
    const scorecardPromise = api.GET('/api/runs/{run_id}/scorecard', {
      params: { path: { run_id: runId } },
    });

    const runResult = await runPromise;
    if (get().runId !== runId) return; // superseded by a newer loadRun call
    if (runResult.error) {
      set({ runStatus: 'error', runError: describeError(runResult.error) });
    } else {
      set({ run: runResult.data, runStatus: 'loaded' });
    }

    void waterfallPromise.then((result) => {
      if (get().runId !== runId) return;
      if (result.error) {
        set({ waterfallStatus: 'error', waterfallError: describeError(result.error) });
      } else {
        set({ waterfall: result.data, waterfallStatus: 'loaded' });
      }
    });

    void scorecardPromise.then((result) => {
      if (get().runId !== runId) return;
      if (result.error) {
        const status = result.response.status === 409 ? 'unavailable' : 'error';
        set({ scorecardStatus: status, scorecardError: describeError(result.error) });
        return;
      }
      const parsed = parseScorecardPayload(result.data);
      if (parsed === null) {
        set({
          scorecardStatus: 'error',
          scorecardError: 'Scorecard payload did not match the expected §17.4 shape.',
        });
        return;
      }
      set({ scorecard: parsed, scorecardStatus: 'loaded' });
    });
  },
});

/**
 * Every non-2xx response is `{"error": {"code", "message", "detail", "docs"}}`
 * (`src/agentdx/api/errors.py`'s `_envelope`, installed for every exception the API raises).
 */
function describeError(error: unknown): string {
  if (error && typeof error === 'object' && 'error' in error) {
    const inner = (error as { error?: unknown }).error;
    if (inner && typeof inner === 'object' && 'message' in inner) {
      const message = (inner as { message?: unknown }).message;
      if (typeof message === 'string') return message;
    }
  }
  return 'Request failed.';
}

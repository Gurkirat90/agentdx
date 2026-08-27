/**
 * `chaosSlice` (PRD §28.2: "Armed fault, resolved blast radius, fire state. Two-step arm/fire
 * [SOURCE]"). Not one of P15's four named slices — P16 scope.
 *
 * The two-step **arm -> confirm -> fire** flow (PRD §28.3, §29.7, I12) is split across two
 * kinds of state on purpose: `staged` is pure client-side selection (no network call, freely
 * reversible — this is the "arm" the UI shows the blast radius for, before anything is
 * committed) and `fire()` is the one call that reaches the network, matching PRD §26.1's
 * `POST /api/runs/{id}/faults` — whose own response field is literally `armed_at_virtual_ts`,
 * because arming and firing are the same server call once `trigger.immediate: true` is set.
 * There is no separate "fire" endpoint to call (CONTEXT.md: no new API endpoints in P16) —
 * "confirm" is a client-only gate in front of the one real network call this slice makes,
 * never a second server round trip.
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import { parseResolvedScenario, type ResolvedFault, type ResolvedScenario } from '../api/scenario';
import type { FetchStatus } from './types';

export type FireStatus = 'idle' | 'firing' | 'fired' | 'error';

export interface ChaosSlice {
  scenario: ResolvedScenario | null;
  scenarioStatus: FetchStatus;
  scenarioError: string | null;
  /** OP-3 repair (2026-08-25): which `runId` `scenario`/`scenarioStatus` currently describe —
   * see `graphSlice.ts`'s `graphRunId` for the full rationale. Keyed on `runId` rather than
   * `scenarioId` (the value `loadScenario` actually fetches by) because the `runId` prop
   * `RunRoute` passes down is never itself stale — `run?.scenario.id` is, since `run` is only
   * reset by `runSlice.loadRun`'s own effect, on the same one-render-late schedule this
   * repair exists to route around; comparing against `runId` sidesteps that second layer of
   * staleness instead of trusting a value that has the same problem this fix is for. */
  scenarioRunId: string | null;

  /** The armed-but-not-fired fault, or `null` — client-local, never sent until `fireStaged`
   * is called. Setting this is the "arm" step; PRD requires the blast radius to be shown
   * before firing, which the panel renders directly from `scenario.blastRadius` while this
   * is non-null. */
  staged: ResolvedFault | null;
  fireStatus: FireStatus;
  fireError: string | null;
  lastFired: { fault_id: string; armed_at_virtual_ts: number } | null;

  loadScenario: (runId: string, scenarioId: string | null) => Promise<void>;
  arm: (fault: ResolvedFault) => void;
  disarm: () => void;
  fireStaged: (runId: string) => Promise<void>;
}

export const createChaosSlice: StateCreator<ChaosSlice, [], [], ChaosSlice> = (set, get) => ({
  scenario: null,
  scenarioStatus: 'idle',
  scenarioError: null,
  scenarioRunId: null,

  staged: null,
  fireStatus: 'idle',
  fireError: null,
  lastFired: null,

  /**
   * `scenarioId` is `run.scenario.id` (`RunDetail.scenario.id`, `GET /api/runs/{id}`) —
   * nullable for a run whose scenario was never recorded (declared gap, same shape the
   * backend's own I12 fail-open-bug repair had to reason about, CONTEXT.md §11 tripwire 18).
   * When it is `null` this deliberately does **not** attempt to fire anything: I12 exists to
   * refuse an unverifiable authorization, and this panel refuses the same way client-side
   * rather than relying on the backend's own narrower open gap (a `scenario_id`-less run is
   * not exploitable through this UI, even though the one shipped route path that creates a
   * run always sets it today).
   */
  loadScenario: async (runId, scenarioId) => {
    set({ staged: null, fireStatus: 'idle', fireError: null, lastFired: null, scenarioRunId: runId });
    if (scenarioId === null) {
      set({ scenario: null, scenarioStatus: 'loaded', scenarioError: null });
      return;
    }
    set({ scenarioStatus: 'loading', scenarioError: null });
    const result = await api.GET('/api/scenarios/{scenario_id}', {
      params: { path: { scenario_id: scenarioId } },
    });
    if (get().scenarioRunId !== runId) return; // superseded by a newer loadScenario call
    // See the identical comment in `graphSlice.ts`'s `loadGraph`: `result.error` alone is not
    // a reliable success guard against `openapi-fetch` returning neither `error` nor `data`.
    if (result.error || result.data === undefined) {
      set({ scenarioStatus: 'error', scenarioError: 'Could not load this run\'s scenario.' });
      return;
    }
    const parsed = parseResolvedScenario(result.data.resolved);
    if (parsed === null) {
      set({ scenarioStatus: 'error', scenarioError: 'Scenario did not resolve to a valid document.' });
      return;
    }
    set({ scenario: parsed, scenarioStatus: 'loaded' });
  },

  arm: (fault) => set({ staged: fault, fireStatus: 'idle', fireError: null }),
  disarm: () => set({ staged: null, fireStatus: 'idle', fireError: null }),

  fireStaged: async (runId) => {
    const { staged } = get();
    if (staged === null) return;
    set({ fireStatus: 'firing', fireError: null });
    const result = await api.POST('/api/runs/{run_id}/faults', {
      params: { path: { run_id: runId } },
      body: {
        type: staged.type,
        target: staged.target,
        params: staged.params as Record<string, never>,
        trigger: { immediate: true },
      },
    });
    if (result.error || result.data === undefined) {
      set({ fireStatus: 'error', fireError: describeFaultError(result.error) });
      return;
    }
    set({
      fireStatus: 'fired',
      lastFired: { fault_id: result.data.fault_id, armed_at_virtual_ts: result.data.armed_at_virtual_ts },
      staged: null,
    });
  },
});

function describeFaultError(error: unknown): string {
  if (error && typeof error === 'object' && 'error' in error) {
    const inner = (error as { error?: unknown }).error;
    if (inner && typeof inner === 'object' && 'message' in inner) {
      const message = (inner as { message?: unknown }).message;
      if (typeof message === 'string') return message;
    }
  }
  if (typeof error === 'object' && error !== null && 'detail' in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  return 'Fault injection failed.';
}

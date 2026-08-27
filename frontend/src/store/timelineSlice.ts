/**
 * `timelineSlice` (PRD §28.2) — "The single source of 'when'." P15 built `virtualTs`/
 * `playing`/`speed`/`mode` so the Waterfall panel's scrub-position rule and hover/click
 * behaviour had one real place to read/write virtual time. P16 adds the scrubber component
 * itself (`Timeline.tsx`) plus state reconstruction at the current scrub position (PRD §20.4,
 * FR-10) — extending this slice, not forking a second "when".
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import type { FetchStatus, StateResponse } from './types';

export type TimelineMode = 'live' | 'replay';

export interface TimelineSlice {
  /** `null` = no scrub position set; nothing has been hovered/clicked yet. */
  virtualTs: number | null;
  playing: boolean;
  speed: number;
  mode: TimelineMode;
  setVirtualTs: (virtualTs: number | null) => void;
  setPlaying: (playing: boolean) => void;
  setSpeed: (speed: number) => void;
  setMode: (mode: TimelineMode) => void;

  // --- P16: state reconstruction at the current scrub position (PRD §20.4, FR-10) ---------
  stateAt: StateResponse | null;
  stateAtStatus: FetchStatus;
  stateAtError: string | null;
  /** Reconstructs state at `virtualTs` via `GET /api/runs/{id}/state` — the same snapshot +
   * replay endpoint FR-10's <200ms p95 targets (`bench/harness/scrub_reconstruction.py`
   * benchmarks the backend function this endpoint calls directly). Callers debounce
   * (`Timeline.tsx`'s `useDebouncedStateAt`) so a fast drag does not fire one request per
   * animation frame — PRD §28.5's `requestAnimationFrame` batching governs the scrub-line
   * redraw itself, which is a pure client-side reflow and needs no debounce of its own. */
  loadStateAt: (runId: string, virtualTs: number) => Promise<void>;
}

export const createTimelineSlice: StateCreator<TimelineSlice, [], [], TimelineSlice> = (
  set,
  get,
) => ({
  virtualTs: null,
  playing: false,
  speed: 1,
  mode: 'replay',
  setVirtualTs: (virtualTs) => set({ virtualTs }),
  setPlaying: (playing) => set({ playing }),
  setSpeed: (speed) => set({ speed }),
  setMode: (mode) => set({ mode }),

  stateAt: null,
  stateAtStatus: 'idle',
  stateAtError: null,

  loadStateAt: async (runId, virtualTs) => {
    set({ stateAtStatus: 'loading', stateAtError: null });
    const result = await api.GET('/api/runs/{run_id}/state', {
      params: { path: { run_id: runId }, query: { at_virtual_ts: virtualTs } },
    });
    // A later call may have started (and finished) after this one while it was in flight —
    // never let a slower, stale response overwrite a newer scrub position's result.
    if (get().virtualTs !== virtualTs) return;
    if (result.error) {
      set({ stateAtStatus: 'error', stateAtError: 'State reconstruction failed.' });
      return;
    }
    set({ stateAt: result.data, stateAtStatus: 'loaded' });
  },
});

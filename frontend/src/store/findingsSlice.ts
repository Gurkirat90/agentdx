/**
 * `findingsSlice` (PRD §28.2) — findings, filters, `includeSuppressed`. The `FindingsPanel`
 * itself is P16 scope (out of this prompt's DELIVERABLES), but the slice is named in this
 * prompt's own deliverable list, so it is built now against the real
 * `GET /api/runs/{id}/findings` endpoint: P16 wires a panel to it with no store change, and
 * `EvidenceLink` (built now, `components/`) can already resolve a `finding_id` to something
 * real when a caller has one.
 */
import type { StateCreator } from 'zustand';

import { api } from '../api/client';
import type { FetchStatus, FindingOut } from './types';

export interface FindingsFilters {
  severity: string[];
  type: string[];
}

export interface FindingsSlice {
  findings: FindingOut[];
  findingsStatus: FetchStatus;
  findingsError: string | null;
  /** OP-3 repair (2026-08-25): which `runId` `findings`/`findingsStatus` currently describe —
   * see `graphSlice.ts`'s `graphRunId` for the full rationale (the identical structural gap,
   * found by inspection once `graphSlice`'s instance turned out to be real). */
  findingsRunId: string | null;
  filters: FindingsFilters;
  includeSuppressed: boolean;
  loadFindings: (runId: string) => Promise<void>;
  setFilters: (filters: Partial<FindingsFilters>) => void;
  setIncludeSuppressed: (includeSuppressed: boolean) => void;
}

export const createFindingsSlice: StateCreator<FindingsSlice, [], [], FindingsSlice> = (
  set,
  get,
) => ({
  findings: [],
  findingsStatus: 'idle',
  findingsError: null,
  findingsRunId: null,
  filters: { severity: [], type: [] },
  includeSuppressed: false,

  loadFindings: async (runId) => {
    set({ findingsStatus: 'loading', findingsError: null, findingsRunId: runId });
    // P16: always fetch the full universe (including guard-suppressed conflicts) once — the
    // `filters`/`includeSuppressed` state below is applied client-side by `FindingsPanel`
    // (`visibleFindings`/`suppressedFindings`, `panels/Findings.tsx`), so toggling a filter
    // never re-fetches, and the "suppressed (n)" drawer (PRD §28.3) always has real data to
    // show rather than an empty array until a second request lands.
    const result = await api.GET('/api/runs/{run_id}/findings', {
      params: { path: { run_id: runId }, query: { include_suppressed: true } },
    });
    if (get().findingsRunId !== runId) return; // superseded by a newer loadFindings call
    // See the identical comment in `graphSlice.ts`'s `loadGraph`: `result.error` alone is not
    // a reliable success guard against `openapi-fetch` returning neither `error` nor `data`.
    if (result.error || result.data === undefined) {
      set({ findingsStatus: 'error', findingsError: 'Request failed.' });
      return;
    }
    set({ findings: [...result.data.findings], findingsStatus: 'loaded' });
  },

  setFilters: (filters) => set({ filters: { ...get().filters, ...filters } }),
  setIncludeSuppressed: (includeSuppressed) => set({ includeSuppressed }),
});

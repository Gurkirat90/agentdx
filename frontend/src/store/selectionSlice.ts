/**
 * `selectionSlice` (PRD §28.2) — "the single source of 'what'": every highlight in every
 * panel derives from this, never from local component state (AGENTS.md §4, CONTEXT.md §11
 * tripwire 8: "a panel starts owning selection state instead of the Zustand store").
 */
import type { StateCreator } from 'zustand';

import type { FindingsSlice } from './findingsSlice';
import { evidenceEventSeqs, evidenceSpanIds } from './linking';
import type { RunSlice } from './runSlice';
import type { TimelineSlice } from './timelineSlice';
import type { Selection } from './types';

export interface SelectionSlice {
  selection: Selection | null;
  select: (selection: Selection) => void;
  clearSelection: () => void;
  /**
   * `selectFinding` — PRD §20.6: "selecting one seeks the timeline to
   * `min(event_a.virtual_ts, event_b.virtual_ts)` and marks both events on the scrubber."
   * This build has no per-event `virtual_ts` fetch cheaply available at selection time (the
   * full event log is not loaded — PRD §20.1's own lazy-loading rationale) — the already-
   * loaded waterfall's span `start_ms` (already virtual time, I11) is used as the seek
   * position instead: the state op an evidence `seq` names always occurs inside the span
   * that was open at the time (the same relationship `gen/make_fixtures.py`'s `span_ids`
   * resolution relies on), so its span's `start_ms` differs from the op's own exact
   * `virtual_ts_ms` by at most that span's own duration — immaterial at scrubber
   * granularity. A P16 simplification, declared rather than silent (avoids a per-click
   * `GET .../events` round trip this build's DELIVERABLES do not ask for).
   */
  selectFinding: (findingId: string) => void;
}

export const createSelectionSlice: StateCreator<
  SelectionSlice & Pick<FindingsSlice, 'findings'> & Pick<RunSlice, 'waterfall'> & Pick<TimelineSlice, 'virtualTs'>,
  [],
  [],
  SelectionSlice
> = (set, get) => ({
  selection: null,
  select: (selection) => set({ selection }),
  clearSelection: () => set({ selection: null }),

  selectFinding: (findingId) => {
    const { findings, waterfall } = get();
    const finding = findings.find((f) => f.id === findingId);
    const seqs = finding ? evidenceEventSeqs(finding) : [];
    const spanIds = finding ? evidenceSpanIds(finding) : [];
    let virtualTs: number | null = null;
    if (waterfall !== null && spanIds.length > 0) {
      const starts = waterfall.lanes
        .flatMap((lane) => lane.spans)
        .filter((s) => spanIds.includes(s.span_id))
        .map((s) => s.start_ms);
      if (starts.length > 0) virtualTs = Math.min(...starts);
    }
    set({
      selection: { kind: 'finding', id: findingId },
      ...(virtualTs !== null ? { virtualTs } : {}),
    });
    void seqs; // event_seqs are exposed via `highlightedEventSeqs` (linking.ts) for markers
  },
});

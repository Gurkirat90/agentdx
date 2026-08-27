/**
 * The Control Tower's single Zustand store — P15's four slices (`run`, `selection`,
 * `timeline`, `findings`) plus P16's three (`graph`, `chaos`, `events` — PRD §28.2). Panels
 * select from this store; they never hold selection or derived state themselves (AGENTS.md
 * §4, CONTEXT.md §11 tripwire 8).
 */
import { create } from 'zustand';

import { createChaosSlice, type ChaosSlice } from './chaosSlice';
import { createEventsSlice, type EventsSlice } from './eventsSlice';
import { createFindingsSlice, type FindingsSlice } from './findingsSlice';
import { createGraphSlice, type GraphSlice } from './graphSlice';
import { createRunSlice, type RunSlice } from './runSlice';
import { createSelectionSlice, type SelectionSlice } from './selectionSlice';
import { createTimelineSlice, type TimelineSlice } from './timelineSlice';

export type ControlTowerStore = RunSlice &
  SelectionSlice &
  TimelineSlice &
  FindingsSlice &
  GraphSlice &
  ChaosSlice &
  EventsSlice;

export const useControlTowerStore = create<ControlTowerStore>()((...a) => ({
  ...createRunSlice(...a),
  ...createSelectionSlice(...a),
  ...createTimelineSlice(...a),
  ...createFindingsSlice(...a),
  ...createGraphSlice(...a),
  ...createChaosSlice(...a),
  ...createEventsSlice(...a),
}));

export * from './types';
export type { FindingsFilters } from './findingsSlice';
export type { TimelineMode } from './timelineSlice';
export type { FireStatus } from './chaosSlice';
export type { WsStatus } from './eventsSlice';
export * from './linking';

/** Shared store types (PRD §28.2). Kept separate so panels can import types without
 * importing the store implementation. */
import type { components } from '../api/client';

export type RunDetail = components['schemas']['RunDetail'];
export type WaterfallResponse = components['schemas']['WaterfallResponse'];
export type WaterfallLane = components['schemas']['WaterfallLane'];
export type WaterfallSpan = components['schemas']['WaterfallSpan'];
export type FindingOut = components['schemas']['FindingOut'];

// --- P16 additions --------------------------------------------------------------------
export type GraphResponse = components['schemas']['GraphResponse'];
export type GraphNode = components['schemas']['GraphNode'];
export type GraphEdge = components['schemas']['GraphEdge'];
export type StateResponse = components['schemas']['StateResponse'];
export type StateKey = components['schemas']['StateKey'];
export type ScenarioDetail = components['schemas']['ScenarioDetail'];
export type ScenarioValidateResponse = components['schemas']['ScenarioValidateResponse'];
export type FaultInjectRequest = components['schemas']['FaultInjectRequest'];
export type FaultInjectResponse = components['schemas']['FaultInjectResponse'];
/** One event, on the wire exactly as `events.canonical.encode_event` serialises it (WS
 * `"event"`/`"events"` frames and `GET .../events` share this shape — PRD §26.1, §26.2). */
export interface LiveEvent {
  schema_version: number;
  run_id: string;
  seq: number;
  sched_step: number;
  virtual_ts_ms: number;
  wall_ts_ms: number;
  vclock: Record<string, number>;
  type: string;
  causal_parents: number[];
  payload: Record<string, unknown>;
  agent_id: string | null;
  clock_slot: string | null;
  span_id: string | null;
  fault_id: string | null;
}

export type FetchStatus = 'idle' | 'loading' | 'loaded' | 'error';
export type ScorecardStatus = 'idle' | 'loading' | 'loaded' | 'unavailable' | 'error';

/** `selectionSlice`'s shape (PRD §28.2): the single source of "what" every panel highlights
 * from. `null` means nothing selected. */
export type SelectionKind = 'span' | 'event' | 'finding' | 'agent' | 'edge';

export interface Selection {
  kind: SelectionKind;
  id: string;
}

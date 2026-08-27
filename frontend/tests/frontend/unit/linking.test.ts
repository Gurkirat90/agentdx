import { describe, expect, it } from 'vitest';

import {
  evidenceAgents,
  evidenceEventSeqs,
  evidenceFaultId,
  evidenceKey,
  evidenceSpanIds,
  findingById,
  highlightedAgentIds,
  highlightedEdge,
  highlightedEventSeqs,
  highlightedSpanIds,
  rankFindings,
  suppressedFindings,
  visibleFindings,
  type LinkingSource,
} from '../../../src/store/linking';
import type { FindingOut, GraphResponse, WaterfallResponse } from '../../../src/store/types';

/**
 * OP-3 repair (2026-08-24, following an independent post-build review). `linking.ts` shipped
 * (P16) with zero unit tests — a declared gap (CONTEXT.md §7 "Blocked on") — exercised only
 * indirectly by `crossHighlight.spec.ts` against a fixture with exactly one finding. The
 * review's own test-quality finding: a single-finding e2e test can't distinguish "read the
 * two agents from this finding's own evidence" from "returned two hardcoded strings that
 * happen to match this one fixture's real values" — nor can it distinguish `findingById`
 * correctly locating the clicked finding from a bug that always returns `findings[0]`.
 *
 * These tests use **two** hand-authored findings with deliberately different agents, spans,
 * seqs and evidence shapes — the whole point is that a wrong-finding or hardcoded-value bug
 * has somewhere to go wrong that a one-finding test cannot expose.
 */

function finding(overrides: Partial<FindingOut> & { id: string }): FindingOut {
  return {
    type: 'state_conflict',
    subtype: null,
    severity: 'high',
    title: 'test finding',
    description: 'test finding',
    evidence: {},
    recommendation: null,
    repro_scenario: null,
    suppressed_by: null,
    ...overrides,
  };
}

const F1 = finding({
  id: 'f_lost_update',
  severity: 'critical',
  evidence: {
    span_ids: ['span_a1', 'span_a2'],
    event_seqs: [13, 28],
    agent_a: 'coder',
    agent_b: 'reviewer',
    key: 'draft.module_a',
    fault_id: null,
  },
});

const F2 = finding({
  id: 'f_stale_read',
  severity: 'medium',
  evidence: {
    span_ids: ['span_b1'],
    event_seqs: [99],
    agent_a: 'planner',
    agent_b: 'worker',
    key: null,
    fault_id: 'flt_001',
  },
});

const FINDINGS: FindingOut[] = [F1, F2];

const GRAPH: GraphResponse = {
  nodes: [
    { id: 'coder', role: null },
    { id: 'reviewer', role: null },
    { id: 'planner', role: null },
    { id: 'worker', role: null },
  ],
  edges: [
    { from: 'coder', to: 'reviewer', message_count: 3 },
    { from: 'planner', to: 'worker', message_count: 1 },
  ],
  critical_path: [],
} as unknown as GraphResponse;

const WATERFALL: WaterfallResponse = {
  virtual_makespan_ms: 100,
  baseline_makespan_ms: null,
  lanes: [
    { agent: 'coder', spans: [{ span_id: 'span_a1' }] },
    { agent: 'reviewer', spans: [{ span_id: 'span_a2' }] },
    { agent: 'planner', spans: [{ span_id: 'span_b1' }] },
  ],
} as unknown as WaterfallResponse;

function sourceFor(selection: LinkingSource['selection']): LinkingSource {
  return { selection, findings: FINDINGS, waterfall: WATERFALL, graph: GRAPH };
}

describe('linking.ts evidence accessors', () => {
  it('findingById locates the clicked finding by id, not by position', () => {
    // A `findings[0]`-style bug would return F1 for both calls; the second assertion is what
    // catches it.
    expect(findingById(FINDINGS, 'f_lost_update')).toBe(F1);
    expect(findingById(FINDINGS, 'f_stale_read')).toBe(F2);
    expect(findingById(FINDINGS, 'f_missing')).toBeNull();
  });

  it("reads each finding's OWN agent pair — not a hardcoded pair that happens to match one fixture", () => {
    expect(evidenceAgents(F1)).toEqual({ agentA: 'coder', agentB: 'reviewer' });
    expect(evidenceAgents(F2)).toEqual({ agentA: 'planner', agentB: 'worker' });
  });

  it("reads each finding's own span ids and event seqs, not a shared/first-finding default", () => {
    expect(evidenceSpanIds(F1)).toEqual(['span_a1', 'span_a2']);
    expect(evidenceSpanIds(F2)).toEqual(['span_b1']);
    expect(evidenceEventSeqs(F1)).toEqual([13, 28]);
    expect(evidenceEventSeqs(F2)).toEqual([99]);
  });

  it('evidenceKey/evidenceFaultId are per-finding and correctly absent where the fixture has none', () => {
    expect(evidenceKey(F1)).toBe('draft.module_a');
    expect(evidenceKey(F2)).toBeNull();
    expect(evidenceFaultId(F1)).toBeNull();
    expect(evidenceFaultId(F2)).toBe('flt_001');
  });
});

describe('linking.ts selection-driven highlight sets (PRD §20.3/§29.7, gate G8)', () => {
  it('a finding selection highlights exactly that finding\'s two agents — switching findings changes the set', () => {
    expect(highlightedAgentIds(sourceFor({ kind: 'finding', id: 'f_lost_update' }))).toEqual(
      new Set(['coder', 'reviewer']),
    );
    expect(highlightedAgentIds(sourceFor({ kind: 'finding', id: 'f_stale_read' }))).toEqual(
      new Set(['planner', 'worker']),
    );
  });

  it("a finding selection highlights exactly that finding's own evidence spans", () => {
    expect(highlightedSpanIds(sourceFor({ kind: 'finding', id: 'f_lost_update' }))).toEqual(
      new Set(['span_a1', 'span_a2']),
    );
    expect(highlightedSpanIds(sourceFor({ kind: 'finding', id: 'f_stale_read' }))).toEqual(
      new Set(['span_b1']),
    );
  });

  it("a finding selection highlights exactly that finding's own evidence event seqs", () => {
    expect(highlightedEventSeqs(sourceFor({ kind: 'finding', id: 'f_lost_update' }))).toEqual(
      new Set([13, 28]),
    );
    expect(highlightedEventSeqs(sourceFor({ kind: 'finding', id: 'f_stale_read' }))).toEqual(
      new Set([99]),
    );
  });

  it('highlightedEdge resolves the real graph edge between the two agents, per finding', () => {
    expect(highlightedEdge(sourceFor({ kind: 'finding', id: 'f_lost_update' }))).toEqual({
      from: 'coder',
      to: 'reviewer',
    });
    expect(highlightedEdge(sourceFor({ kind: 'finding', id: 'f_stale_read' }))).toEqual({
      from: 'planner',
      to: 'worker',
    });
  });

  it('a span/event/agent selection (not a finding) highlights only the one thing selected', () => {
    expect(highlightedSpanIds(sourceFor({ kind: 'span', id: 'span_a1' }))).toEqual(new Set(['span_a1']));
    expect(highlightedAgentIds(sourceFor({ kind: 'agent', id: 'planner' }))).toEqual(new Set(['planner']));
    expect(highlightedEventSeqs(sourceFor({ kind: 'event', id: '99' }))).toEqual(new Set([99]));
    // Selecting a single span must not also highlight the OTHER finding's agents/spans.
    expect(highlightedAgentIds(sourceFor({ kind: 'span', id: 'span_a1' })).size).toBe(0);
  });

  it('no selection highlights nothing', () => {
    const src = sourceFor(null);
    expect(highlightedAgentIds(src).size).toBe(0);
    expect(highlightedSpanIds(src).size).toBe(0);
    expect(highlightedEventSeqs(src).size).toBe(0);
    expect(highlightedEdge(src)).toBeNull();
  });
});

describe('linking.ts findings ranking/filtering', () => {
  it('rankFindings orders by severity (most severe first), id as tiebreak', () => {
    expect(rankFindings(FINDINGS).map((f) => f.id)).toEqual(['f_lost_update', 'f_stale_read']);
  });

  it('visibleFindings excludes suppressed findings; suppressedFindings is the complement', () => {
    const suppressed = finding({
      id: 'f_suppressed',
      severity: 'low',
      suppressed_by: 'declared_reducer',
      evidence: { agent_a: 'coder', agent_b: 'worker' },
    });
    const all = [...FINDINGS, suppressed];
    const filters = { severity: [], type: [] };
    expect(visibleFindings(all, filters).map((f) => f.id)).toEqual(['f_lost_update', 'f_stale_read']);
    expect(suppressedFindings(all, filters).map((f) => f.id)).toEqual(['f_suppressed']);
  });
});

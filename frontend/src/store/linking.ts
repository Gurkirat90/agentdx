/**
 * The cross-panel linking model (PRD §20.3, §28.2, gate G8). Built once, here, before any of
 * P16's panels — every panel derives its highlight from these pure selectors over the store,
 * never from local component state (CONTEXT.md §11 tripwire 8).
 *
 * `selectionSlice.selection` is still the single `{kind, id}` source of "what" (P15). What
 * this module adds is the *derivation*: a `finding` selection is one id, but PRD §20.3/§29.7
 * require it to highlight **two spans in the waterfall and two nodes in the graph
 * simultaneously** — "cross-panel linking is the whole reason for three panels." Rather than
 * widening `Selection` into a multi-id shape (which every existing P15 selection site would
 * have to be rewritten around, CONTEXT.md §2's scope discipline), a `finding` selection stays
 * one id and these selectors *expand* it, by looking up the finding's own evidence — the same
 * pattern `EvidenceLink` (P15) already uses one level down (a `span` selection is already
 * exactly what a waterfall span highlights on; this only adds the finding -> spans/agents/edge
 * expansion on top).
 */
import type { FindingOut, GraphResponse, Selection, WaterfallResponse } from './types';

export interface LinkingSource {
  selection: Selection | null;
  findings: readonly FindingOut[];
  waterfall: WaterfallResponse | null;
  graph: GraphResponse | null;
}

/** A finding's evidence, as this build's own P16 ruling shapes it (`gen/make_fixtures.py`,
 * CONTEXT.md ruling C-32) — read defensively, since `FindingOut.evidence` is an untyped
 * `dict[str, JsonValue]` server-side (no schema enforces this shape beyond I6's "non-empty"). */
function evidenceArray(evidence: Record<string, unknown>, key: string): string[] {
  const value = evidence[key];
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === 'string' || typeof v === 'number').map(String);
}

export function findingById(findings: readonly FindingOut[], id: string): FindingOut | null {
  return findings.find((f) => f.id === id) ?? null;
}

/** One finding's evidence span ids (P16 ruling C-32's `span_ids` field). */
export function evidenceSpanIds(finding: FindingOut): string[] {
  return evidenceArray(finding.evidence, 'span_ids');
}

/** One finding's evidence event seqs (P16 ruling C-32's `event_seqs` field, PRD §26.1). */
export function evidenceEventSeqs(finding: FindingOut): number[] {
  return evidenceArray(finding.evidence, 'event_seqs').map(Number);
}

function evidenceString(finding: FindingOut, key: string): string | null {
  const value = finding.evidence[key];
  return typeof value === 'string' ? value : null;
}

/** The two agents a finding's evidence names (C-32's `agent_a`/`agent_b`), for the Findings
 * panel's own display — `highlightedAgentIds` above derives the *highlight set* from the same
 * fields for the graph, this is the ordered pair for a "coder vs reviewer" label. */
export function evidenceAgents(finding: FindingOut): { agentA: string | null; agentB: string | null } {
  return { agentA: evidenceString(finding, 'agent_a'), agentB: evidenceString(finding, 'agent_b') };
}

/** The state key a `state_conflict` finding's evidence names (C-32's `key` field), or `null`
 * for a finding shape that carries none. */
export function evidenceKey(finding: FindingOut): string | null {
  return evidenceString(finding, 'key');
}

/** The fault id a finding's evidence names (C-32's `fault_id` field) — non-null means this
 * finding is fault-tainted: it may be a consequence of an injected fault rather than an organic
 * defect, which the Findings panel must surface (Design Constraint 5's "fault taint status"),
 * never silently fold into the same presentation as an untainted finding. */
export function evidenceFaultId(finding: FindingOut): string | null {
  return evidenceString(finding, 'fault_id');
}

/** Every span id (waterfall) a selection should highlight. */
export function highlightedSpanIds(src: LinkingSource): ReadonlySet<string> {
  const { selection } = src;
  if (selection === null) return EMPTY_SET;
  if (selection.kind === 'span') return new Set([selection.id]);
  if (selection.kind === 'finding') {
    const finding = findingById(src.findings, selection.id);
    if (finding === null) return EMPTY_SET;
    return new Set(evidenceSpanIds(finding));
  }
  return EMPTY_SET;
}

/** Every event `seq` a selection should highlight (waterfall/timeline markers). */
export function highlightedEventSeqs(src: LinkingSource): ReadonlySet<number> {
  const { selection } = src;
  if (selection === null) return EMPTY_NUM_SET;
  if (selection.kind === 'event') {
    const n = Number(selection.id);
    return Number.isFinite(n) ? new Set([n]) : EMPTY_NUM_SET;
  }
  if (selection.kind === 'finding') {
    const finding = findingById(src.findings, selection.id);
    if (finding === null) return EMPTY_NUM_SET;
    return new Set(evidenceEventSeqs(finding));
  }
  return EMPTY_NUM_SET;
}

/** Every agent id (graph nodes) a selection should highlight. */
export function highlightedAgentIds(src: LinkingSource): ReadonlySet<string> {
  const { selection } = src;
  if (selection === null) return EMPTY_SET;
  if (selection.kind === 'agent') return new Set([selection.id]);
  if (selection.kind === 'finding') {
    const finding = findingById(src.findings, selection.id);
    if (finding === null) return EMPTY_SET;
    const evidence = finding.evidence;
    const agents = [evidence.agent_a, evidence.agent_b].filter((a): a is string => typeof a === 'string');
    if (agents.length > 0) return new Set(agents);
    // Fallback for a finding whose evidence carries no explicit agent_a/agent_b (a shape this
    // build's own ruling always populates, but a future producer might not): resolve agents
    // from the highlighted spans via the loaded waterfall, so linking degrades gracefully
    // rather than breaking outright.
    const spanIds = highlightedSpanIds(src);
    if (spanIds.size === 0 || src.waterfall === null) return EMPTY_SET;
    const resolved = new Set<string>();
    for (const lane of src.waterfall.lanes) {
      if (lane.spans.some((s) => spanIds.has(s.span_id))) resolved.add(lane.agent);
    }
    return resolved;
  }
  return EMPTY_SET;
}

/** The one graph edge (`{from,to}`) a selection should highlight, or `null`. Only meaningful
 * for a two-agent finding whose two agents share a real edge in the loaded graph. */
export function highlightedEdge(src: LinkingSource): { from: string; to: string } | null {
  const agents = highlightedAgentIds(src);
  if (agents.size !== 2 || src.graph === null) return null;
  const [a, b] = [...agents];
  const edge = src.graph.edges.find(
    (e) => (e.from === a && e.to === b) || (e.from === b && e.to === a),
  );
  return edge ? { from: edge.from, to: edge.to } : null;
}

/** The finding id a selection represents, or `null` — for the Findings panel's own row
 * highlight (selecting a span/agent does not highlight a finding row; only the reverse). */
export function selectedFindingId(selection: Selection | null): string | null {
  return selection?.kind === 'finding' ? selection.id : null;
}

const EMPTY_SET: ReadonlySet<string> = new Set();
const EMPTY_NUM_SET: ReadonlySet<number> = new Set();

// --- Findings ranking/filtering (PRD §28.3: "severity-ranked", §14.7's suppressed drawer) --

/** PRD §29.6's severity order, most severe first — the same rank the Findings panel's list
 * and its `j`/`k` navigation (§20.6) both use. */
export const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? SEVERITY_RANK.info!;
}

/** Severity-ranked, then id (stable, deterministic — never insertion order alone). */
export function rankFindings(findings: readonly FindingOut[]): FindingOut[] {
  return [...findings].sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || a.id.localeCompare(b.id));
}

export interface FindingsFiltersLike {
  severity: readonly string[];
  type: readonly string[];
}

function matchesFilters(finding: FindingOut, filters: FindingsFiltersLike): boolean {
  const severityOk = filters.severity.length === 0 || filters.severity.includes(finding.severity);
  const typeOk = filters.type.length === 0 || filters.type.includes(finding.type);
  return severityOk && typeOk;
}

/** Findings the panel's main list renders: filtered, never guard-suppressed, severity-ranked. */
export function visibleFindings(
  findings: readonly FindingOut[],
  filters: FindingsFiltersLike,
): FindingOut[] {
  return rankFindings(findings.filter((f) => f.suppressed_by === null && matchesFilters(f, filters)));
}

/** Findings the "suppressed (n)" drawer (§14.7) renders — filtered the same way, but only
 * the guard-suppressed ones, and only when the drawer is open (`includeSuppressed`). */
export function suppressedFindings(
  findings: readonly FindingOut[],
  filters: FindingsFiltersLike,
): FindingOut[] {
  return rankFindings(findings.filter((f) => f.suppressed_by !== null && matchesFilters(f, filters)));
}

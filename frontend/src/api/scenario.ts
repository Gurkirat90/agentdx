/**
 * `ScenarioDetail.resolved` / `ScenarioValidateResponse.resolved` (the GENERATED type) is
 * `{ [key: string]: unknown }` — deliberately untyped, same reasoning as `scorecard.ts`:
 * `scenario.loader.resolve_defaults` returns a plain resolved YAML document, and PRD §21.1 is
 * the schema, not a typed API model. This file is the same kind of documented, narrow ruling
 * `scorecard.ts` already sets precedent for — derived from PRD §21.1's literal worked example
 * and `scenario/schema.py`'s real field names (`TARGET_FIELD`: `agent`/`edge`/`tool`/
 * `state_key`/`provider` singular on a fault entry; `_BLAST_RADIUS_FIELD`: `agents`/`edges`/
 * `tools`/`state_keys`/`providers` plural on `blast_radius`) — not invented.
 *
 * The Chaos panel (PRD §28.3, §13.4) needs exactly three things out of a resolved scenario:
 * which faults are declared, what blast radius (if any) covers them, and whether this is a
 * `target.graph` scenario that I12 actually gates (`chaos_opt_in`/`blast_radius` are legally
 * absent/empty for a `target.fixture` scenario — `scenario/validate.py`'s own `E-SCEN-004`
 * scoping — and this parser must not read that as "no blast radius was declared" the way it
 * would for a graph target).
 */
import type { components } from './schema';

export type ResolvedScenarioRaw = NonNullable<
  components['schemas']['ScenarioValidateResponse']['resolved']
>;

export type FaultTargetKind = 'agent' | 'edge' | 'tool' | 'state_key' | 'provider';

export const FAULT_TARGET_FIELDS: readonly FaultTargetKind[] = [
  'agent',
  'edge',
  'tool',
  'state_key',
  'provider',
];

/** One `faults[]` entry (PRD §21.1), narrowed to what the Chaos panel renders and fires. */
export interface ResolvedFault {
  type: string;
  targetKind: FaultTargetKind;
  target: string;
  /** Every other key on the fault entry (e.g. `recoverable`, `at_virtual_ts`) — passed
   * through to `POST /api/runs/{id}/faults`'s `params`, minus the trigger-only keys this
   * module knows about. A key `FAULT_CATALOGUE` (`scenario/schema.py`) does not accept for
   * this fault type is refused server-side (`400`), not silently dropped here. */
  params: Record<string, unknown>;
}

export interface ResolvedBlastRadius {
  agents: string[];
  edges: string[];
  tools: string[];
  state_keys: string[];
  providers: string[];
}

export interface ResolvedScenario {
  scenarioId: string | null;
  targetKind: 'fixture' | 'graph' | 'unknown';
  targetName: string | null;
  chaosOptIn: boolean;
  /** `null` when the document declares no `blast_radius` block at all (legal for a
   * `target.fixture` scenario) — distinct from an explicitly-empty one, so the panel can say
   * "not declared" rather than implying an empty radius was measured and found to be zero. */
  blastRadius: ResolvedBlastRadius | null;
  faults: ResolvedFault[];
}

/** Trigger-shaped keys this module knows are never a fault `param` (PRD §12.3's trigger
 * vocabulary plus §21.1's `agent`/`edge`/... target keys) — stripped from `params` before it
 * is sent to `POST /faults`. Not derived from `FAULT_CATALOGUE` (not exposed over the API,
 * CONTEXT.md §4: `api/` serves resolved documents, not the Python fault catalogue directly) —
 * a declared simplification, not a silent one. */
const NON_PARAM_KEYS = new Set([
  'type',
  ...FAULT_TARGET_FIELDS,
  'at_virtual_ts',
  'after_n_messages',
  'first_n',
  'always',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function parseFault(raw: unknown): ResolvedFault | null {
  if (!isRecord(raw) || typeof raw.type !== 'string') return null;
  for (const kind of FAULT_TARGET_FIELDS) {
    const value = raw[kind];
    if (typeof value === 'string') {
      const params: Record<string, unknown> = {};
      for (const [key, val] of Object.entries(raw)) {
        if (!NON_PARAM_KEYS.has(key)) params[key] = val;
      }
      return { type: raw.type, targetKind: kind, target: value, params };
    }
  }
  return null;
}

function parseBlastRadius(raw: unknown): ResolvedBlastRadius | null {
  if (!isRecord(raw)) return null;
  return {
    agents: asStringArray(raw.agents),
    edges: asStringArray(raw.edges),
    tools: asStringArray(raw.tools),
    state_keys: asStringArray(raw.state_keys),
    providers: asStringArray(raw.providers),
  };
}

export function blastRadiusIsEmpty(radius: ResolvedBlastRadius): boolean {
  return (
    radius.agents.length === 0 &&
    radius.edges.length === 0 &&
    radius.tools.length === 0 &&
    radius.state_keys.length === 0 &&
    radius.providers.length === 0
  );
}

/** Parse a resolved scenario document. Returns `null` only when `raw` is not even an object —
 * every real, schema-valid resolved scenario (PRD §21.1) parses, since every field this
 * function reads is optional-with-a-documented-default here, matching `resolve_defaults`'s own
 * behaviour of filling every key in before returning. */
export function parseResolvedScenario(raw: unknown): ResolvedScenario | null {
  if (!isRecord(raw)) return null;
  const target = isRecord(raw.target) ? raw.target : {};
  const targetKind = typeof target.fixture === 'string' ? 'fixture' : typeof target.graph === 'string' ? 'graph' : 'unknown';
  const targetName =
    typeof target.fixture === 'string' ? target.fixture : typeof target.graph === 'string' ? target.graph : null;
  const faults = Array.isArray(raw.faults) ? raw.faults.map(parseFault).filter((f): f is ResolvedFault => f !== null) : [];
  return {
    scenarioId: typeof raw.scenario === 'string' ? raw.scenario : null,
    targetKind,
    targetName,
    chaosOptIn: raw.chaos_opt_in === true,
    blastRadius: 'blast_radius' in raw ? parseBlastRadius(raw.blast_radius) : null,
    faults,
  };
}

import { useEffect } from 'react';

import { Badge } from '../components/Badge';
import { cx } from '../lib/cx';
import { blastRadiusIsEmpty, type ResolvedBlastRadius, type ResolvedFault, type ResolvedScenario } from '../api/scenario';
import { useControlTowerStore } from '../store';
import styles from './Chaos.module.css';

/**
 * Chaos control panel (PRD §28.3, §13.3–13.6, I12, Design Constraint 2). **Arm -> confirm ->
 * fire, always**: `arm()` only ever stages a fault client-side (no network call); `fireStaged()`
 * is the one real request, and it is only reachable through the confirm view below, which
 * renders the full resolved blast radius before its Fire button exists in the DOM at all — there
 * is no code path in this panel that reaches `fireStaged` without that render having happened
 * first. This mirrors I12/§13.4 rule 4 ("the resolved blast radius is displayed in the UI before
 * a fault can be fired") and the backend's own two real gates (`E-SCEN-004` at validation,
 * `E-CHAOS-001` at runtime) — this panel's own `isChaosAuthorized` below is a third, client-side
 * gate on top of both, refusing to even offer "Arm" for a fault this run's own resolved scenario
 * would not survive firing, so a user is never invited into a confirm step that can only end in
 * a rejected request.
 */
export function ChaosPanel({ runId }: { runId: string }): React.JSX.Element {
  const scenario = useControlTowerStore((s) => s.scenario);
  const scenarioStatus = useControlTowerStore((s) => s.scenarioStatus);
  const scenarioError = useControlTowerStore((s) => s.scenarioError);
  const scenarioRunId = useControlTowerStore((s) => s.scenarioRunId);
  const staged = useControlTowerStore((s) => s.staged);
  const fireStatus = useControlTowerStore((s) => s.fireStatus);
  const fireError = useControlTowerStore((s) => s.fireError);
  const lastFired = useControlTowerStore((s) => s.lastFired);
  // `runSlice.runId` — the route's own run id (set by `loadRun`, `RunRoute.tsx`'s `useEffect`)
  // — not `run?.run_id` (the server-echoed field on the fetched `RunDetail`): in production the
  // two always agree, but nothing in this store enforces that, and `fireStaged` must fire
  // against the run this page is actually showing. Named `fireRunId` (not `runId`) because this
  // component now also takes the route's own `runId` as a prop (OP-3 repair, 2026-08-25, see
  // below) — the two are expected to agree once `loadRun`'s effect has caught up, but
  // `fireStaged` intentionally keeps reading the store's own copy rather than the prop, since it
  // must never fire against a run this panel hasn't actually confirmed loading for yet.
  const fireRunId = useControlTowerStore((s) => s.runId);
  const arm = useControlTowerStore((s) => s.arm);
  const disarm = useControlTowerStore((s) => s.disarm);
  const fireStaged = useControlTowerStore((s) => s.fireStaged);

  // OP-3 repair (2026-08-25): see `GraphPanel`'s identical guard (`Graph.tsx`) and
  // `graphSlice.ts`'s `graphRunId` docstring for the full rationale. Compared against the
  // `runId` *prop* rather than the store's own `runId` above — `s.runId` has the same
  // one-render-late staleness this guard exists to route around.
  if (scenarioRunId !== runId || scenarioStatus === 'idle' || scenarioStatus === 'loading') {
    return (
      <section className={styles.panel} aria-label="Chaos control" aria-busy="true">
        <p className={styles.status}>Loading scenario…</p>
      </section>
    );
  }
  if (scenarioStatus === 'error') {
    return (
      <section className={styles.panel} aria-label="Chaos control">
        <p className={styles.status} role="alert">
          {scenarioError ?? 'Scenario failed to load.'}
        </p>
      </section>
    );
  }
  if (scenario === null) {
    // I12: no `scenario_id` on this run means chaos-authorization cannot be verified for it
    // (CONTEXT.md §11 tripwire 18's own "narrower gap ... unreachable through the shipped
    // POST /api/runs today, but a real gap if some other path ever creates a run without one").
    // This panel refuses the same way client-side, even though today's only run-creation path
    // always sets one — see `chaosSlice.ts::loadScenario`'s own docstring for the full reasoning.
    return (
      <section className={styles.panel} aria-label="Chaos control">
        <p className={styles.status}>
          This run has no recorded scenario — chaos controls are unavailable (I12: firing
          requires a verifiable, resolved scenario).
        </p>
      </section>
    );
  }

  const authorized = isChaosAuthorized(scenario);

  return (
    <section className={styles.panel} aria-label="Chaos control">
      <div className={styles.header}>
        <h1 className={styles.title}>Chaos Control</h1>
        <TargetBadge scenario={scenario} />
      </div>

      {!authorized ? (
        <p className={styles.status} role="note">
          {scenario.targetKind === 'graph'
            ? 'This scenario targets a user graph without chaos_opt_in and a non-empty blast_radius (I12) — firing would be refused server-side (E-SCEN-004/E-CHAOS-001), so arming is disabled here too.'
            : 'This scenario\'s target could not be classified as a fixture or an opted-in graph — arming is refused (fail closed).'}
        </p>
      ) : null}

      {lastFired !== null ? (
        <p className={styles.lastFired} data-testid="chaos-last-fired">
          Last fired: <span className="numeric">{lastFired.fault_id}</span> at virtual{' '}
          <span className="numeric">{lastFired.armed_at_virtual_ts}</span>ms
        </p>
      ) : null}

      {staged !== null ? (
        <ArmedConfirm
          fault={staged}
          scenario={scenario}
          fireStatus={fireStatus}
          fireError={fireError}
          onFire={() => fireRunId !== null && void fireStaged(fireRunId)}
          onDisarm={disarm}
          canFire={fireRunId !== null}
        />
      ) : (
        <FaultCatalogue faults={scenario.faults} authorized={authorized} onArm={arm} />
      )}
    </section>
  );
}

function isChaosAuthorized(scenario: ResolvedScenario): boolean {
  // §13.3: a fixture target is permitted by default (its blast radius defaults to "everything
  // in the fixture" — chaos_opt_in/blast_radius are legally absent for it, not a failure to
  // declare them). A graph target needs both chaos_opt_in and a real, non-empty blast radius.
  // An `unknown` target kind fails closed — the same posture I12 takes for an unverifiable run.
  if (scenario.targetKind === 'fixture') return true;
  if (scenario.targetKind === 'graph') {
    return scenario.chaosOptIn && scenario.blastRadius !== null && !blastRadiusIsEmpty(scenario.blastRadius);
  }
  return false;
}

function TargetBadge({ scenario }: { scenario: ResolvedScenario }): React.JSX.Element {
  if (scenario.targetKind === 'unknown') {
    return <Badge tone="crit">target unknown</Badge>;
  }
  return (
    <Badge tone={scenario.targetKind === 'fixture' ? 'ok' : 'warn'}>
      {scenario.targetKind}
      {scenario.targetName ? `: ${scenario.targetName}` : ''}
    </Badge>
  );
}

function FaultCatalogue({
  faults,
  authorized,
  onArm,
}: {
  faults: ResolvedFault[];
  authorized: boolean;
  onArm: (fault: ResolvedFault) => void;
}): React.JSX.Element {
  if (faults.length === 0) {
    return <p className={styles.status}>This scenario declares no faults.</p>;
  }
  return (
    <ul className={styles.faultList}>
      {faults.map((fault, i) => (
        <li key={`${fault.type}-${fault.targetKind}-${fault.target}-${i}`} className={styles.faultRow}>
          <div className={styles.faultInfo}>
            <span className={cx(styles.faultType, 'numeric')}>{fault.type}</span>
            <span className={styles.faultTarget}>
              {fault.targetKind}: <span className="numeric">{fault.target}</span>
            </span>
          </div>
          <button
            type="button"
            data-testid="chaos-arm-button"
            className={styles.armButton}
            disabled={!authorized}
            onClick={() => onArm(fault)}
          >
            Arm
          </button>
        </li>
      ))}
    </ul>
  );
}

function ArmedConfirm({
  fault,
  scenario,
  fireStatus,
  fireError,
  onFire,
  onDisarm,
  canFire,
}: {
  fault: ResolvedFault;
  scenario: ResolvedScenario;
  fireStatus: 'idle' | 'firing' | 'fired' | 'error';
  fireError: string | null;
  onFire: () => void;
  onDisarm: () => void;
  canFire: boolean;
}): React.JSX.Element {
  const firing = fireStatus === 'firing';
  return (
    <div className={styles.confirm} data-testid="chaos-armed" role="group" aria-label="Armed fault, awaiting confirmation">
      <p className={styles.armedLabel}>ARMED — not yet fired</p>
      <dl className={styles.faultDetail}>
        <dt>Type</dt>
        <dd className="numeric">{fault.type}</dd>
        <dt>Target</dt>
        <dd className="numeric">
          {fault.targetKind}: {fault.target}
        </dd>
        {Object.entries(fault.params).map(([key, value]) => (
          <FaultParam key={key} paramKey={key} value={value} />
        ))}
      </dl>

      <BlastRadiusDisplay scenario={scenario} />

      {fireStatus === 'error' && fireError !== null ? (
        <p className={styles.fireError} role="alert">
          {fireError}
        </p>
      ) : null}

      <div className={styles.confirmActions}>
        <button
          type="button"
          data-testid="chaos-disarm-button"
          className={styles.disarmButton}
          onClick={onDisarm}
          disabled={firing}
        >
          Disarm
        </button>
        <button
          type="button"
          data-testid="chaos-fire-button"
          className={styles.fireButton}
          onClick={onFire}
          disabled={firing || !canFire}
        >
          {firing ? 'Firing…' : 'Fire'}
        </button>
      </div>
    </div>
  );
}

function FaultParam({ paramKey, value }: { paramKey: string; value: unknown }): React.JSX.Element {
  return (
    <>
      <dt>{paramKey}</dt>
      <dd className="numeric">{typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : JSON.stringify(value)}</dd>
    </>
  );
}

/** §13.4 rule 4: "The resolved blast radius is displayed in the UI before a fault can be
 * fired" — every category rendered, always, even the empty ones (an omitted category would
 * read as "not checked" rather than "confirmed empty"). A `null` radius on a fixture target
 * gets §13.3's own literal fallback text, not a blank section. */
function BlastRadiusDisplay({ scenario }: { scenario: ResolvedScenario }): React.JSX.Element {
  if (scenario.blastRadius === null) {
    return (
      <div className={styles.blastRadius} data-testid="chaos-blast-radius">
        <p className={styles.blastRadiusTitle}>Blast radius</p>
        <p className={styles.blastRadiusFixtureNote}>
          fixture sandbox — no explicit blast_radius declared; §13.3 default applies: everything
          in the fixture is in scope
        </p>
      </div>
    );
  }
  return (
    <div className={styles.blastRadius} data-testid="chaos-blast-radius">
      <p className={styles.blastRadiusTitle}>Blast radius</p>
      <BlastRadiusCategory label="agents" items={scenario.blastRadius.agents} />
      <BlastRadiusCategory label="edges" items={scenario.blastRadius.edges} />
      <BlastRadiusCategory label="tools" items={scenario.blastRadius.tools} />
      <BlastRadiusCategory label="state keys" items={scenario.blastRadius.state_keys} />
      <BlastRadiusCategory label="providers" items={scenario.blastRadius.providers} />
    </div>
  );
}

function BlastRadiusCategory({ label, items }: { label: string; items: readonly string[] }): React.JSX.Element {
  return (
    <div className={styles.blastRadiusRow}>
      <span className={styles.blastRadiusLabel}>{label}</span>
      {items.length === 0 ? (
        <span className={styles.blastRadiusEmpty}>none</span>
      ) : (
        <span className={cx(styles.blastRadiusItems, 'numeric')}>{items.join(', ')}</span>
      )}
    </div>
  );
}

/** Ties this panel's scenario fetch to the run's own scenario id (`RunDetail.scenario.id`),
 * mirroring `useLoadGraph`/`useLoadFindings`. `null` is a legitimate value (a scenario-less
 * run) and is passed straight through — `loadScenario(runId, null)` refuses to enable firing
 * itself (`chaosSlice.ts`), it does not skip the call.
 *
 * OP-3 repair (2026-08-25): now also takes `runId`, threaded straight from `RunRoute`'s own
 * prop rather than read back off `run?.scenario.id` — `chaosSlice.ts`'s `scenarioRunId`
 * docstring explains why the run id itself, not the scenario id derived from `run`, is the
 * correlation key `ChaosPanel`'s render guard needs. */
export function useLoadScenario(runId: string, scenarioId: string | null): void {
  const loadScenario = useControlTowerStore((s) => s.loadScenario);
  useEffect(() => {
    void loadScenario(runId, scenarioId);
  }, [runId, scenarioId, loadScenario]);
}

export type { ResolvedBlastRadius };

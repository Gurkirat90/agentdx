import { useEffect, useMemo, useState } from 'react';

import { Badge } from '../components/Badge';
import { EvidenceLink } from '../components/EvidenceLink';
import { SeverityDot, type Severity } from '../components/SeverityDot';
import { cx } from '../lib/cx';
import {
  evidenceAgents,
  evidenceEventSeqs,
  evidenceFaultId,
  evidenceKey,
  evidenceSpanIds,
  selectedFindingId,
  suppressedFindings,
  visibleFindings,
  useControlTowerStore,
} from '../store';
import type { FindingOut } from '../store/types';
import styles from './Findings.module.css';

const ALL_SEVERITIES: Severity[] = ['critical', 'high', 'medium', 'low', 'info'];

/**
 * Findings panel (PRD §28.3, §20.6, Design Constraint 5): severity-ranked list, each row
 * showing classification (type/subtype), both conflicting events (`EvidenceLink`, I6 — never a
 * bare count), a copyable minimal repro command, and fault taint status. Clicking a row is the
 * literal G8 gate action: it drives the whole cross-panel highlight via `selectFinding`
 * (`selectionSlice.ts`) — this panel itself holds no highlight state of its own.
 */
export function FindingsPanel({ runId }: { runId: string }): React.JSX.Element {
  const findings = useControlTowerStore((s) => s.findings);
  const findingsStatus = useControlTowerStore((s) => s.findingsStatus);
  const findingsError = useControlTowerStore((s) => s.findingsError);
  const findingsRunId = useControlTowerStore((s) => s.findingsRunId);
  const filters = useControlTowerStore((s) => s.filters);
  const setFilters = useControlTowerStore((s) => s.setFilters);
  const includeSuppressed = useControlTowerStore((s) => s.includeSuppressed);
  const setIncludeSuppressed = useControlTowerStore((s) => s.setIncludeSuppressed);
  const selection = useControlTowerStore((s) => s.selection);
  const selectFinding = useControlTowerStore((s) => s.selectFinding);

  const activeId = selectedFindingId(selection);
  const visible = useMemo(() => visibleFindings(findings, filters), [findings, filters]);
  const suppressed = useMemo(() => suppressedFindings(findings, filters), [findings, filters]);

  const availableTypes = useMemo(
    () => [...new Set(findings.map((f) => f.type))].sort(),
    [findings],
  );

  // OP-3 repair (2026-08-25): see `GraphPanel`'s identical guard (`Graph.tsx`) and
  // `graphSlice.ts`'s `graphRunId` docstring for the full rationale.
  if (findingsRunId !== runId || findingsStatus === 'idle' || findingsStatus === 'loading') {
    return (
      <section className={styles.panel} aria-label="Findings" aria-busy="true">
        <p className={styles.status}>Loading findings…</p>
      </section>
    );
  }
  if (findingsStatus === 'error') {
    return (
      <section className={styles.panel} aria-label="Findings">
        <p className={styles.status} role="alert">
          {findingsError ?? 'Findings failed to load.'}
        </p>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-label="Findings">
      <div className={styles.header}>
        <h1 className={styles.title}>Findings</h1>
        <span className={cx(styles.count, 'numeric')}>{visible.length}</span>
      </div>

      <FindingsFilterBar
        availableTypes={availableTypes}
        filters={filters}
        setFilters={setFilters}
      />

      {findings.length === 0 ? (
        // I10: absence of findings is a coverage statement, never rendered as an empty list
        // indistinguishable from "not loaded yet" — this build's own analysis ran and found
        // nothing to report for this run.
        <p className={styles.status}>
          No findings for this run — bounded search found no conflicts (I10: absence of findings
          is not proof of absence).
        </p>
      ) : visible.length === 0 ? (
        <p className={styles.status}>No findings match the current filters.</p>
      ) : (
        <ul className={styles.list}>
          {visible.map((finding) => (
            <FindingRow
              key={finding.id}
              finding={finding}
              active={finding.id === activeId}
              onSelect={() => selectFinding(finding.id)}
            />
          ))}
        </ul>
      )}

      <SuppressedDrawer
        suppressed={suppressed}
        includeSuppressed={includeSuppressed}
        setIncludeSuppressed={setIncludeSuppressed}
        activeId={activeId}
        onSelect={selectFinding}
      />
    </section>
  );
}

interface FindingsFilterBarProps {
  availableTypes: string[];
  filters: { severity: string[]; type: string[] };
  setFilters: (filters: Partial<{ severity: string[]; type: string[] }>) => void;
}

function FindingsFilterBar({
  availableTypes,
  filters,
  setFilters,
}: FindingsFilterBarProps): React.JSX.Element {
  const toggleSeverity = (severity: Severity): void => {
    const next = filters.severity.includes(severity)
      ? filters.severity.filter((s) => s !== severity)
      : [...filters.severity, severity];
    setFilters({ severity: next });
  };
  const toggleType = (type: string): void => {
    const next = filters.type.includes(type)
      ? filters.type.filter((t) => t !== type)
      : [...filters.type, type];
    setFilters({ type: next });
  };

  return (
    <div className={styles.filterBar} role="group" aria-label="Findings filters">
      <div className={styles.filterGroup} role="group" aria-label="Filter by severity">
        {ALL_SEVERITIES.map((severity) => (
          <button
            key={severity}
            type="button"
            className={cx(
              styles.filterChip,
              filters.severity.includes(severity) && styles.filterChipActive,
            )}
            aria-pressed={filters.severity.includes(severity)}
            onClick={() => toggleSeverity(severity)}
          >
            <SeverityDot severity={severity} />
            {severity}
          </button>
        ))}
      </div>
      {availableTypes.length > 0 ? (
        <div className={styles.filterGroup} role="group" aria-label="Filter by type">
          {availableTypes.map((type) => (
            <button
              key={type}
              type="button"
              className={cx(
                styles.filterChip,
                filters.type.includes(type) && styles.filterChipActive,
              )}
              aria-pressed={filters.type.includes(type)}
              onClick={() => toggleType(type)}
            >
              {type}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FindingRow({
  finding,
  active,
  onSelect,
}: {
  finding: FindingOut;
  active: boolean;
  onSelect: () => void;
}): React.JSX.Element {
  const { agentA, agentB } = evidenceAgents(finding);
  const key = evidenceKey(finding);
  const faultId = evidenceFaultId(finding);
  const seqs = evidenceEventSeqs(finding);
  const spanIds = evidenceSpanIds(finding);

  return (
    <li>
      <button
        type="button"
        data-testid="finding-row"
        data-finding-id={finding.id}
        className={cx(styles.row, active && styles.rowActive)}
        onClick={onSelect}
        aria-pressed={active}
      >
        <div className={styles.rowTop}>
          <SeverityDot severity={finding.severity as Severity} showLabel />
          <Badge tone="neutral">
            {finding.type}
            {finding.subtype ? ` / ${finding.subtype}` : ''}
          </Badge>
          <FaultTaintBadge faultId={faultId} />
        </div>
        <p className={styles.rowTitle}>{finding.title}</p>
        {agentA !== null && agentB !== null ? (
          <p className={styles.rowAgents}>
            {agentA} ↔ {agentB}
            {key !== null ? <span className={cx(styles.rowKey, 'numeric')}> · {key}</span> : null}
          </p>
        ) : null}
        <div className={styles.rowEvidence} onClick={(e) => e.stopPropagation()}>
          <EvidenceLink seqs={seqs} spanIds={spanIds} />
        </div>
        {finding.recommendation !== null ? (
          <p className={styles.rowRecommendation}>{finding.recommendation}</p>
        ) : null}
      </button>
      <ReproCommand finding={finding} />
    </li>
  );
}

/** Design Constraint 5's "fault taint status" — always rendered, both states, never omitted
 * when absent (an omitted badge would read as "unknown" rather than "confirmed untainted"). */
function FaultTaintBadge({ faultId }: { faultId: string | null }): React.JSX.Element {
  if (faultId === null) {
    return <Badge tone="ok">no fault</Badge>;
  }
  return (
    <Badge tone="fault" srPrefix="fault-tainted">
      fault {faultId}
    </Badge>
  );
}

/**
 * The copyable minimal repro command (Design Constraint 5). Grounded in the PRD's own literal
 * command form (§14.8, line "Reproduce: agentdx run scenarios/repro_f_0117.yaml") built from
 * `FindingOut.repro_scenario` (`repro_scenario_path` server-side). §14.8's minimal-repro
 * *generator* — the thing that actually populates this path — is not this build's scope (a
 * later prompt owns it, `gen/make_fixtures.py`'s own header names the gap), so every finding in
 * this build's real fixtures carries `repro_scenario: null` honestly; this component states
 * that plainly rather than fabricating a command PRD §14.8 has not actually generated yet.
 */
function ReproCommand({ finding }: { finding: FindingOut }): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  if (finding.repro_scenario === null) {
    return (
      <p className={styles.reproMissing}>
        no repro scenario generated yet (§14.8's generator is not built in this release)
      </p>
    );
  }
  const command = `agentdx run ${finding.repro_scenario}`;
  const copy = (): void => {
    void navigator.clipboard
      ?.writeText(command)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  };
  return (
    <div className={styles.repro}>
      <code className={cx(styles.reproCommand, 'numeric')}>{command}</code>
      <button type="button" className={styles.reproCopy} onClick={copy}>
        {copied ? 'copied' : 'copy'}
      </button>
    </div>
  );
}

function SuppressedDrawer({
  suppressed,
  includeSuppressed,
  setIncludeSuppressed,
  activeId,
  onSelect,
}: {
  suppressed: FindingOut[];
  includeSuppressed: boolean;
  setIncludeSuppressed: (v: boolean) => void;
  activeId: string | null;
  onSelect: (id: string) => void;
}): React.JSX.Element | null {
  if (suppressed.length === 0) return null;
  return (
    <div className={styles.suppressedDrawer}>
      <button
        type="button"
        className={styles.suppressedToggle}
        aria-expanded={includeSuppressed}
        onClick={() => setIncludeSuppressed(!includeSuppressed)}
      >
        Suppressed ({suppressed.length})
      </button>
      {includeSuppressed ? (
        <ul className={styles.list}>
          {suppressed.map((finding) => (
            <li key={finding.id}>
              <button
                type="button"
                data-testid="finding-row-suppressed"
                data-finding-id={finding.id}
                className={cx(styles.row, styles.rowSuppressed, finding.id === activeId && styles.rowActive)}
                onClick={() => onSelect(finding.id)}
              >
                <div className={styles.rowTop}>
                  <SeverityDot severity={finding.severity as Severity} showLabel />
                  <Badge tone="neutral">{finding.type}</Badge>
                  <Badge tone="neutral">suppressed by {finding.suppressed_by}</Badge>
                </div>
                <p className={styles.rowTitle}>{finding.title}</p>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Ties this panel's initial fetch to the run id, mirroring `useLoadGraph`. */
export function useLoadFindings(runId: string): void {
  const loadFindings = useControlTowerStore((s) => s.loadFindings);
  useEffect(() => {
    void loadFindings(runId);
  }, [runId, loadFindings]);
}

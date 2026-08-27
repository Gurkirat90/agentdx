import styles from './Waterfall.module.css';

export interface GhostBaselineProps {
  /** Pixel x position of the baseline finish, or `null` while pending. */
  x: number | null;
  height: number;
  /** Multi-agent achieved speedup, e.g. 0.83 → "0.83×". `null` while pending. */
  speedup: number | null;
  comparabilityGrade: 'A' | 'B' | 'C' | null;
}

/**
 * THE SIGNATURE ELEMENT (PRD §29.4). A dashed vertical line marking where the single-agent
 * baseline finished, rendered *behind* the multi-agent waterfall so the comparison reads at
 * a glance — every bar extending past this line is overhead made visible.
 *
 * Two states, both spec-required: while `x`/`speedup` are `null` (today's real backend
 * always returns `baseline_makespan_ms: null` — no baseline-execution pipeline exists yet,
 * P17/RunHost gap, declared not hidden) this renders the "pending ghost with a spinner"
 * PRD §29.4 itself specifies for exactly this situation — never a fabricated line. Once a
 * real number exists, it renders the full labelled dashed line with speedup and grade.
 */
export function GhostBaseline({
  x,
  height,
  speedup,
  comparabilityGrade,
}: GhostBaselineProps): React.JSX.Element {
  if (x === null || speedup === null) {
    return (
      <g className={styles.ghostPending} data-testid="ghost-baseline-pending">
        <text x={8} y={14} className={styles.ghostPendingLabel}>
          <tspan className={styles.spinner} aria-hidden="true">
            ◐
          </tspan>{' '}
          single-agent baseline — not yet computed
        </text>
      </g>
    );
  }

  const flag = speedup < 1;
  return (
    <g data-testid="ghost-baseline" className={styles.ghost}>
      <line x1={x} x2={x} y1={0} y2={height} className={styles.ghostLine} />
      <text x={x + 6} y={14} className={styles.ghostLabel}>
        single-agent baseline{comparabilityGrade ? ` · grade ${comparabilityGrade}` : ''} ·{' '}
        <tspan className={`numeric ${flag ? styles.ghostSlower : styles.ghostFaster}`}>
          {speedup.toFixed(2)}×
        </tspan>
      </text>
    </g>
  );
}

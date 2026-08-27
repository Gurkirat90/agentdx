import { Bar } from '../components/Bar';
import { EvidenceLink } from '../components/EvidenceLink';
import { useControlTowerStore } from '../store';
import { bucketLabel } from './scorecardLabels';
import styles from './Scorecard.module.css';

/**
 * The PRD §17.4 canonical scorecard, rendered with every number linked to its evidence
 * (`EvidenceLink`) and the comparability grade adjacent to the speedup, never a footnote
 * (§28.3 "Scorecard"). Comparability grading is I3/G6's own thing to compute — this panel
 * only ever displays a grade the API returned, never infers one.
 */
export function ScorecardPanel(): React.JSX.Element {
  const scorecard = useControlTowerStore((s) => s.scorecard);
  const scorecardStatus = useControlTowerStore((s) => s.scorecardStatus);
  const scorecardError = useControlTowerStore((s) => s.scorecardError);

  if (scorecardStatus === 'idle' || scorecardStatus === 'loading') {
    return (
      <section className={styles.panel} aria-label="Scorecard" aria-busy="true">
        <h1 className={styles.panelTitle}>Scorecard</h1>
        <p className={styles.status}>Loading scorecard…</p>
      </section>
    );
  }

  if (scorecardStatus === 'unavailable') {
    return (
      <section className={styles.panel} aria-label="Scorecard">
        <h1 className={styles.panelTitle}>Scorecard</h1>
        <p className={styles.status}>
          No scorecard has been computed for this run yet. `agentdx analyze &lt;run_id&gt;
          --scorecard` once the baseline comparison pipeline has run.
        </p>
      </section>
    );
  }

  if (scorecardStatus === 'error' || scorecard === null) {
    return (
      <section className={styles.panel} aria-label="Scorecard">
        <h1 className={styles.panelTitle}>Scorecard</h1>
        <p className={styles.status} role="alert">
          {scorecardError ?? 'Scorecard failed to load.'}
        </p>
      </section>
    );
  }

  const { speedup, buckets, tokens, comparability } = scorecard;
  const slower = speedup.achieved < 1;

  return (
    <section className={styles.panel} aria-label="Scorecard">
      <h1 className={styles.panelTitle}>Scorecard</h1>
      <div className={styles.headline}>
        <span className={styles.headlineLabel}>Coordination Efficiency</span>
        <span className={`numeric ${styles.headlineValue} ${slower ? styles.slower : styles.faster}`}>
          {speedup.achieved.toFixed(2)}×
        </span>
        <span className={styles.headlineWord}>
          {slower ? 'slower than single-agent' : 'faster than single-agent'}
        </span>
      </div>

      <dl className={styles.rows}>
        <div className={styles.row}>
          <dt>Ideal parallel speedup</dt>
          <dd className="numeric">
            {speedup.ideal_parallel.toFixed(2)}× (total work {speedup.total_work_ms}ms / critical
            path {speedup.critical_path_ms}ms)
          </dd>
        </div>
        <div className={styles.row}>
          <dt>Achieved speedup</dt>
          <dd className="numeric">
            {speedup.achieved.toFixed(2)}× (baseline {speedup.virtual_makespan_baseline_ms}ms /
            multi-agent {speedup.virtual_makespan_multi_ms}ms)
          </dd>
        </div>
        <div className={styles.row}>
          <dt>Overhead cost</dt>
          <dd className="numeric">
            {speedup.overhead_cost >= 0 ? '+' : ''}
            {speedup.overhead_cost.toFixed(2)}×
          </dd>
        </div>
      </dl>

      <ul className={styles.buckets}>
        {buckets.map((b) => (
          <li key={b.bucket} className={styles.bucketRow}>
            <span className={styles.bucketLabel}>{bucketLabel(b.bucket)}</span>
            <span className="numeric">
              {b.gap_contribution >= 0 ? '+' : ''}
              {b.gap_contribution.toFixed(2)}×
            </span>
            <span className="numeric">{b.critical_path_ms}ms</span>
            <EvidenceLink seqs={b.evidence_seq} />
          </li>
        ))}
      </ul>

      <dl className={styles.rows}>
        <div className={styles.row}>
          <dt>Token cost multiplier</dt>
          <dd className="numeric">
            {tokens.cost_multiplier.toFixed(1)}× vs single-agent ({tokens.multi} vs{' '}
            {tokens.baseline})
          </dd>
        </div>
        <div className={styles.row}>
          <dt>Cost efficiency</dt>
          <dd className="numeric">{tokens.cost_efficiency.toFixed(2)}</dd>
        </div>
        {scorecard.wall_makespan_ms !== null ? (
          <div className={styles.row}>
            <dt>Wall time</dt>
            <dd className="numeric">
              {(scorecard.wall_makespan_ms / 1000).toFixed(1)}s virtual makespan{' '}
              {(speedup.virtual_makespan_multi_ms / 1000).toFixed(1)}s
            </dd>
          </div>
        ) : null}
      </dl>

      <div className={styles.comparability}>
        <div className={styles.comparabilityHeader}>
          <span>Comparability</span>
          <span className={`${styles.grade} ${styles[`grade${comparability.grade}`]}`}>
            {comparability.grade}
          </span>
          {comparability.grade === 'C' ? (
            <span className={styles.lowComparability}>low comparability</span>
          ) : null}
        </div>
        <Bar
          label="cache reuse"
          value={comparability.cache_reuse_rate}
          valueText={`${Math.round(comparability.cache_reuse_rate * 100)}%`}
          tone={comparability.grade === 'A' ? 'ok' : comparability.grade === 'B' ? 'warn' : 'crit'}
        />
        <p className={styles.comparabilityDetail}>
          tools {Math.round(comparability.cache_reuse_tool_rate * 100)}%, llm{' '}
          {Math.round(comparability.cache_reuse_llm_rate * 100)}% — {comparability.reason}
        </p>
      </div>
    </section>
  );
}

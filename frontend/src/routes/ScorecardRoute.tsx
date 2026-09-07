import { useEffect } from 'react';

import { PanelErrorBoundary } from '../components/PanelErrorBoundary';
import { ScorecardPanel } from '../panels/Scorecard';
import { useControlTowerStore } from '../store';
import { Link } from './router';
import styles from './RunRoute.module.css';

/** `ScorecardRoute` (PRD §28.1): "full §17.4 with evidence links." */
export function ScorecardRoute({ runId }: { runId: string }): React.JSX.Element {
  const loadRun = useControlTowerStore((s) => s.loadRun);
  const runId_ = useControlTowerStore((s) => s.runId);

  useEffect(() => {
    if (runId_ !== runId) void loadRun(runId);
  }, [runId, runId_, loadRun]);

  return (
    <div className={styles.shell}>
      <header className={styles.topBar} aria-label="Run summary">
        <Link to="/" className={styles.brand}>
          AgentDX
        </Link>
        <span className="numeric">run {runId}</span>
        <Link to={`/runs/${runId}`} className={styles.scorecardLink}>
          Waterfall
        </Link>
      </header>
      {/* D-60's rationale for PanelErrorBoundary ("protects the unrelated, working
       * Waterfall/Scorecard panels P15 shipped" from a sibling panel's crash) applies just as
       * much to this panel's own render-time exceptions — this route was the one place it was
       * never applied (op2-audit-p15.md finding #3). Keyed by runId, matching RunRoute.tsx's
       * pattern, so a crash on one run doesn't poison the boundary for the next. */}
      <main className={styles.main}>
        <PanelErrorBoundary key={runId} panelName="Scorecard">
          <ScorecardPanel />
        </PanelErrorBoundary>
      </main>
    </div>
  );
}

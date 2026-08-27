import { useEffect } from 'react';

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
      <main className={styles.main}>
        <ScorecardPanel />
      </main>
    </div>
  );
}

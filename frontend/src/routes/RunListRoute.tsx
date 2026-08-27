import { useEffect, useState } from 'react';

import { api } from '../api/client';
import type { components } from '../api/client';
import { Link } from './router';
import styles from './RunListRoute.module.css';

type RunSummary = components['schemas']['RunSummary'];

/** `RunListRoute` (PRD §28.1): recent runs, empty state → CLI hint (§29.8's copy rule: empty
 * states point at the next action). */
export function RunListRoute(): React.JSX.Element {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api.GET('/api/runs', {}).then((result) => {
      if (cancelled) return;
      if (result.error) {
        setError('Could not load runs.');
        return;
      }
      setRuns([...result.data.runs]);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className={styles.main} aria-label="Runs">
      <h1 className={styles.title}>AgentDX Control Tower</h1>
      {error ? (
        <p role="alert">{error}</p>
      ) : runs === null ? (
        <p className={styles.hint}>Loading runs…</p>
      ) : runs.length === 0 ? (
        <p className={`${styles.hint} numeric`}>
          No runs yet. <code>agentdx run fixtures/code_pipeline</code> to record your first.
        </p>
      ) : (
        <ul className={styles.list}>
          {runs.map((run) => (
            <li key={run.run_id} className={styles.row}>
              <Link to={`/runs/${run.run_id}`} className={styles.link}>
                <span className="numeric">{run.run_id}</span>
                <span className={styles.meta}>
                  {run.mode} · seed {run.seed} · {run.status}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

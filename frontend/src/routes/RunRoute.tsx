import { useEffect } from 'react';

import { Badge } from '../components/Badge';
import { PanelErrorBoundary } from '../components/PanelErrorBoundary';
import { ChaosPanel, useLoadScenario } from '../panels/Chaos';
import { FindingsPanel, useLoadFindings } from '../panels/Findings';
import { GraphPanel, useLoadGraph } from '../panels/Graph';
import { TimelinePanel } from '../panels/Timeline';
import { WaterfallPanel } from '../panels/Waterfall';
import { useControlTowerStore } from '../store';
import { Link } from './router';
import styles from './RunRoute.module.css';

const VERDICT_TONE: Record<string, 'ok' | 'warn' | 'crit' | 'neutral'> = {
  BENEFICIAL: 'ok',
  NEUTRAL: 'neutral',
  NEGATIVE_SPEEDUP: 'warn',
  COORDINATION_BOTTLENECK: 'warn',
  STATE_CONFLICT_RISK: 'crit',
  UNRELIABLE_TOPOLOGY: 'crit',
  NEGATIVE_CAPABILITY: 'crit',
  BASELINE_FAILED: 'neutral',
  BASELINE_CONTEXT_EXCEEDED: 'neutral',
  INSUFFICIENT_DATA: 'neutral',
};

export function RunRoute({ runId }: { runId: string }): React.JSX.Element {
  const loadRun = useControlTowerStore((s) => s.loadRun);
  const run = useControlTowerStore((s) => s.run);
  const runStatus = useControlTowerStore((s) => s.runStatus);
  const runError = useControlTowerStore((s) => s.runError);
  const connectWs = useControlTowerStore((s) => s.connectWs);
  const disconnectWs = useControlTowerStore((s) => s.disconnectWs);
  const wsStatus = useControlTowerStore((s) => s.wsStatus);
  const wsSamplingN = useControlTowerStore((s) => s.wsSamplingN);

  useEffect(() => {
    void loadRun(runId);
  }, [runId, loadRun]);

  // P16 (PRD §28.3, §28.4 item 3): Graph, Findings and the scenario each fetch their own
  // initial resource independently, matching this route's own "no panel fetches on its own
  // except its initial resource" rule (§28.1) — none of these three waits on the others.
  useLoadGraph(runId);
  useLoadFindings(runId);
  useLoadScenario(runId, run?.scenario.id ?? null);

  // §28.4 item 3: "Open the WebSocket if the run is running; otherwise skip it entirely (a
  // completed run needs no socket)." Re-evaluated whenever `run.status` changes (a run this
  // page opened while running can finish mid-session) and always disconnected on unmount so a
  // route change never leaves an orphaned socket retrying in the background.
  useEffect(() => {
    if (run?.status !== 'running') return;
    connectWs(runId);
    return () => disconnectWs();
  }, [runId, run?.status, connectWs, disconnectWs]);

  return (
    <div className={styles.shell}>
      <header className={styles.topBar} aria-label="Run summary">
        <Link to="/" className={styles.brand}>
          AgentDX
        </Link>
        <span className="numeric">run {runId}</span>
        {run ? (
          <>
            <span className="numeric">{run.mode}</span>
            <span className="numeric">seed {run.seed}</span>
            {run.verdict ? (
              <Badge tone={VERDICT_TONE[run.verdict.class] ?? 'neutral'} srPrefix="Verdict">
                {run.verdict.headline}
              </Badge>
            ) : runStatus === 'loaded' ? (
              <Badge tone="neutral">verdict not yet computed</Badge>
            ) : null}
          </>
        ) : runStatus === 'error' ? (
          <span role="alert">{runError ?? 'Run failed to load.'}</span>
        ) : (
          <span aria-busy="true">Loading…</span>
        )}
        <WsStatusBadge status={wsStatus} samplingN={wsSamplingN} runStatus={run?.status ?? null} />
        <Link to={`/runs/${runId}/scorecard`} className={styles.scorecardLink}>
          Scorecard
        </Link>
      </header>
      {/* PRD §28.1's literal layout tree: an outer vertical split (Graph|Chaos+Findings stack
       * on top, Waterfall below), with the Timeline scrubber bar spanning full width beneath
       * everything — implemented as a grid rather than nested split panes, since nothing in
       * this build's scope calls for user-resizable panes. Chaos and Findings get independent
       * grid areas (not one shared `<Stack>` wrapper) so the single-column breakpoint below can
       * reorder them to match §29.5's information-priority order (Findings before Chaos),
       * which differs from §28.1's own two-column wireframe order (Chaos above Findings). */}
      {/* OP-3 repair (2026-08-24): every boundary below is keyed on `runId`. Navigation
       * between runs is client-side (`routes/router.tsx`'s `pushState`, no reload), so without
       * a key React reuses the same `PanelErrorBoundary` instance across a run change — a
       * crash caught while viewing one run left that panel stuck on its fallback message for
       * every *other* run visited afterward in the same session, since an error boundary's
       * caught-error state has no reset trigger but unmount/remount. Keying on `runId` forces
       * exactly that remount on a run change, without remounting the whole route (and losing
       * unrelated panel-local UI state) the way keying `RunRoute` itself would. */}
      {/* OP-3 repair, part 2 (2026-08-25): keying the boundary alone was necessary but not
       * sufficient. `graph`/`findings`/`scenario` each live in a global (not run-scoped) slice,
       * reset only by that slice's own load effect — which fires one render *after* the fresh
       * boundary/panel above has already committed and rendered once. A previous run's
       * leftover, possibly crash-shaped data was still visible on that first render, so a panel
       * that crashed once could crash again immediately on a perfectly healthy next run,
       * defeating the remount above for a different reason. `GraphPanel`/`FindingsPanel`/
       * `ChaosPanel` now take `runId` as a prop and compare it against a `*RunId` field each
       * slice sets synchronously alongside its data, treating a mismatch as still-loading
       * rather than trusting stale cross-run data — see `graphSlice.ts`'s `graphRunId`
       * docstring for the full account. */}
      <main className={styles.main}>
        <div className={styles.graphArea}>
          <PanelErrorBoundary key={runId} panelName="Agent dependency graph">
            <GraphPanel runId={runId} />
          </PanelErrorBoundary>
        </div>
        <div className={styles.chaosArea}>
          <PanelErrorBoundary key={runId} panelName="Chaos panel">
            <ChaosPanel runId={runId} />
          </PanelErrorBoundary>
        </div>
        <div className={styles.findingsArea}>
          <PanelErrorBoundary key={runId} panelName="Findings panel">
            <FindingsPanel runId={runId} />
          </PanelErrorBoundary>
        </div>
        <div className={styles.waterfallArea}>
          <PanelErrorBoundary key={runId} panelName="Coordination waterfall">
            <WaterfallPanel />
          </PanelErrorBoundary>
        </div>
        <div className={styles.timelineArea}>
          <PanelErrorBoundary key={runId} panelName="Timeline scrubber">
            <TimelinePanel />
          </PanelErrorBoundary>
        </div>
      </main>
    </div>
  );
}

const WS_STATUS_LABEL: Record<string, string> = {
  idle: 'not connected',
  connecting: 'connecting…',
  open: 'live',
  reconnecting: 'reconnecting…',
  polling: 'polling (degraded)',
  closed: 'closed',
};

const WS_STATUS_TONE: Record<string, 'ok' | 'warn' | 'crit' | 'neutral'> = {
  idle: 'neutral',
  connecting: 'neutral',
  open: 'ok',
  reconnecting: 'warn',
  polling: 'warn',
  closed: 'neutral',
};

/** Design Constraint 6: "degrade to polling with visible state" — this badge is that visible
 * state. Rendered only while the run is actually `running` (a completed run never opens a
 * socket at all, §28.4 item 3, so there is nothing to report). */
function WsStatusBadge({
  status,
  samplingN,
  runStatus,
}: {
  status: string;
  samplingN: number | null;
  runStatus: string | null;
}): React.JSX.Element | null {
  if (runStatus !== 'running') return null;
  return (
    <span data-testid="ws-status-badge" data-ws-status={status}>
      <Badge tone={WS_STATUS_TONE[status] ?? 'neutral'} srPrefix="Live updates">
        {WS_STATUS_LABEL[status] ?? status}
        {samplingN !== null ? ` (sampling 1/${samplingN})` : ''}
      </Badge>
    </span>
  );
}

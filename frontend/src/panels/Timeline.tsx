import { useCallback, useEffect, useMemo, useRef } from 'react';

import { cx } from '../lib/cx';
import { evidenceSpanIds, rankFindings, selectedFindingId, useControlTowerStore } from '../store';
import type { FindingOut, StateResponse, WaterfallResponse } from '../store/types';
import styles from './Timeline.module.css';

/** Auto-advance interval while `playing` (PRD §20.2 `space` play/pause) — a fixed wall-clock
 * tick, not a true wall-time-to-virtual-time playback ratio: this build's own `speed` field
 * scales how many *event ticks* (not milliseconds) advance per tick, since virtual time between
 * two adjacent events is not uniform and a literal ms-accurate playback is out of P16's scope
 * (declared simplification, not a silent one). */
const PLAY_TICK_MS = 200;

/**
 * Timeline panel (PRD §28.3, §20.2, §20.4, §29.7): the scrubber over virtual time, fault/
 * finding/ghost-baseline markers, and the reconstructed state table at the current scrub
 * position (§20.4's "graph, state table and message log all reflect the selected instant" —
 * this build has no separate state-table deliverable, so it lives here, next to the control
 * that drives it). Keyboard: `←/→` step one event, `shift+←/→` step ten, `j/k` between findings
 * in rank order, `space` play/pause, `home/end` jump to run bounds — all `[SOURCE]`, §20.2.
 *
 * "One event" step granularity (declared simplification): this build has no slice that loads
 * the full per-run event list (`GET /events` exists per PRD §20.1 but nothing in P15/P16 wires
 * it — a real, named gap, not this panel's to close under DELIVERABLES' scope). Every distinct
 * span boundary (`start_ms`/`end_ms`) already loaded in the waterfall is used as the stepping
 * resolution instead — the same virtual timestamps a user already sees rendered as bar edges.
 */
export function TimelinePanel(): React.JSX.Element {
  const waterfall = useControlTowerStore((s) => s.waterfall);
  const virtualTs = useControlTowerStore((s) => s.virtualTs);
  const setVirtualTs = useControlTowerStore((s) => s.setVirtualTs);
  const playing = useControlTowerStore((s) => s.playing);
  const setPlaying = useControlTowerStore((s) => s.setPlaying);
  const mode = useControlTowerStore((s) => s.mode);
  const setMode = useControlTowerStore((s) => s.setMode);
  const stateAt = useControlTowerStore((s) => s.stateAt);
  const stateAtStatus = useControlTowerStore((s) => s.stateAtStatus);
  const findings = useControlTowerStore((s) => s.findings);
  const selection = useControlTowerStore((s) => s.selection);
  const selectFinding = useControlTowerStore((s) => s.selectFinding);
  const lastFired = useControlTowerStore((s) => s.lastFired);
  // `runSlice.runId` (the route's own run id), not `run?.run_id` — same reasoning as
  // `Chaos.tsx`'s `fireStaged` call site.
  const runId = useControlTowerStore((s) => s.runId);
  const loadStateAt = useControlTowerStore((s) => s.loadStateAt);

  const makespan = waterfall?.virtual_makespan_ms ?? null;
  const ticks = useMemo(() => (waterfall ? eventTicks(waterfall) : []), [waterfall]);
  const ranked = useMemo(() => rankFindings(findings), [findings]);
  const activeFindingId = selectedFindingId(selection);
  // Each finding's own marker position (same evidence-span-start proxy `selectFinding` already
  // uses in `selectionSlice.ts` — not this panel's own invented mapping).
  const findingMarks = useMemo(
    () => (waterfall ? findingMarkerPositions(waterfall, ranked) : new Map<string, number>()),
    [waterfall, ranked],
  );

  // Debounced scrub -> state reconstruction (FR-10, <200ms p95 — measured in
  // `bench/results/`, not asserted here; this is the client-side call site that target
  // governs). 80ms trailing debounce: a fast drag fires one request per pause, not one per
  // animation frame.
  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    if (runId === null || virtualTs === null) return;
    if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      void loadStateAt(runId, virtualTs);
    }, 80);
    return () => {
      if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    };
  }, [runId, virtualTs, loadStateAt]);

  const seek = useCallback(
    (ts: number) => {
      if (makespan === null) return;
      const clamped = Math.max(0, Math.min(makespan, ts));
      setMode('replay');
      setVirtualTs(clamped);
    },
    [makespan, setMode, setVirtualTs],
  );

  const stepEvents = useCallback(
    (delta: number) => {
      if (ticks.length === 0 || makespan === null) return;
      const current = virtualTs ?? 0;
      // Nearest-tick index, then step by `delta` ticks — `←`/`→` always moves to a *different*
      // real event boundary, never a no-op sub-pixel nudge.
      let idx = ticks.findIndex((t) => t >= current);
      if (idx === -1) idx = ticks.length - 1;
      if (ticks[idx] === current) idx += delta > 0 ? 1 : delta < 0 ? -1 : 0;
      else if (delta < 0) idx -= 1;
      const nextIdx = Math.max(0, Math.min(ticks.length - 1, idx + (delta > 0 ? delta - 1 : delta < 0 ? delta + 1 : 0)));
      const next = ticks[Math.max(0, Math.min(ticks.length - 1, nextIdx))];
      if (next !== undefined) seek(next);
    },
    [ticks, makespan, virtualTs, seek],
  );

  const jumpFinding = useCallback(
    (direction: 1 | -1) => {
      if (ranked.length === 0) return;
      const currentIndex = ranked.findIndex((f) => f.id === activeFindingId);
      const nextIndex =
        currentIndex === -1
          ? direction > 0
            ? 0
            : ranked.length - 1
          : (currentIndex + direction + ranked.length) % ranked.length;
      const finding = ranked[nextIndex];
      if (finding) selectFinding(finding.id);
    },
    [ranked, activeFindingId, selectFinding],
  );

  // Playback (`space`): advances through real event ticks at a fixed wall-clock cadence
  // scaled by `speed`, auto-pausing at the end rather than looping — a chaos-debugger replay
  // is not a media player, and a silently-looping scrub would misrepresent "the run ended".
  const speed = useControlTowerStore((s) => s.speed);
  useEffect(() => {
    if (!playing || ticks.length === 0) return;
    const id = window.setInterval(() => {
      const current = virtualTs ?? ticks[0] ?? 0;
      const idx = ticks.findIndex((t) => t >= current);
      const nextIdx = (idx === -1 ? 0 : idx) + Math.max(1, Math.round(speed));
      if (nextIdx >= ticks.length) {
        setPlaying(false);
        const last = ticks[ticks.length - 1];
        if (last !== undefined) seek(last);
        return;
      }
      const next = ticks[nextIdx];
      if (next !== undefined) seek(next);
    }, PLAY_TICK_MS);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-runs per tick via setInterval's own closure over `virtualTs`, not per render
  }, [playing, ticks, speed]);

  // Keyboard controls (§20.2 `[SOURCE]`), scoped to this panel's lifetime and ignored while
  // focus is inside a text input/textarea/contenteditable (a global scrub shortcut must not
  // eat keystrokes meant for, e.g., the Findings filter chips' or repro command's own controls).
  useEffect(() => {
    const handler = (e: KeyboardEvent): void => {
      const active = document.activeElement;
      const typing =
        active instanceof HTMLElement &&
        (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable);
      if (typing) return;
      switch (e.key) {
        case 'ArrowLeft':
          e.preventDefault();
          stepEvents(e.shiftKey ? -10 : -1);
          break;
        case 'ArrowRight':
          e.preventDefault();
          stepEvents(e.shiftKey ? 10 : 1);
          break;
        case 'j':
          e.preventDefault();
          jumpFinding(1);
          break;
        case 'k':
          e.preventDefault();
          jumpFinding(-1);
          break;
        case ' ':
          e.preventDefault();
          setPlaying(!playing);
          break;
        case 'Home':
          e.preventDefault();
          // True run bounds (op2-audit-p16.md finding #4), not the first/last *event tick* —
          // a run's first/last span boundary can start well after 0ms or end well before the
          // makespan (measured against real fixture data: up to 42% short on `support_triage`),
          // so `Home`/`End` must jump to `0`/`makespan` directly. `seek` itself clamps and
          // no-ops while `makespan` is still `null`, so this is safe before the waterfall loads.
          seek(0);
          break;
        case 'End':
          e.preventDefault();
          if (makespan !== null) seek(makespan);
          break;
        default:
          break;
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [stepEvents, jumpFinding, setPlaying, playing, ticks, seek, makespan]);

  if (waterfall === null || makespan === null) {
    return (
      <section className={styles.panel} aria-label="Timeline">
        <p className={styles.status}>Timeline needs the waterfall loaded first.</p>
      </section>
    );
  }

  return (
    <section className={styles.panel} aria-label="Timeline">
      <div className={styles.header}>
        <h1 className={styles.title}>Timeline</h1>
        <div className={styles.transport}>
          <button type="button" className={styles.transportButton} onClick={() => seek(0)}>
            ⏮ home
          </button>
          <button
            type="button"
            className={styles.transportButton}
            data-testid="timeline-play-pause"
            onClick={() => setPlaying(!playing)}
          >
            {playing ? '⏸ pause' : '▶ play'}
          </button>
          <button
            type="button"
            className={styles.transportButton}
            onClick={() => makespan !== null && seek(makespan)}
          >
            end ⏭
          </button>
          <button
            type="button"
            className={cx(styles.transportButton, mode === 'live' && styles.transportButtonActive)}
            onClick={() => {
              setMode('live');
              setVirtualTs(null);
            }}
          >
            live
          </button>
        </div>
      </div>

      <Scrubber
        makespan={makespan}
        virtualTs={virtualTs}
        ghostMs={waterfall.baseline_makespan_ms}
        findings={ranked}
        findingMarks={findingMarks}
        activeFindingId={activeFindingId}
        lastFiredMs={lastFired?.armed_at_virtual_ts ?? null}
        onSeek={seek}
        onSelectFinding={selectFinding}
      />

      <StateTable stateAt={stateAt} status={stateAtStatus} virtualTs={virtualTs} />
    </section>
  );
}

function eventTicks(waterfall: WaterfallResponse): number[] {
  const set = new Set<number>();
  for (const lane of waterfall.lanes) {
    for (const span of lane.spans) {
      set.add(span.start_ms);
      set.add(span.end_ms);
    }
  }
  return [...set].sort((a, b) => a - b);
}

/** Each finding's marker position: `min(start_ms)` across its own evidence spans that actually
 * resolve against the loaded waterfall — a finding whose evidence names no known span (a
 * shape this build's own fixtures never produce, C-32, but a future producer might) is simply
 * omitted from the map rather than mis-plotted at 0ms. */
function findingMarkerPositions(waterfall: WaterfallResponse, findings: readonly FindingOut[]): Map<string, number> {
  const startBySpanId = new Map<string, number>();
  for (const lane of waterfall.lanes) {
    for (const span of lane.spans) startBySpanId.set(span.span_id, span.start_ms);
  }
  const marks = new Map<string, number>();
  for (const finding of findings) {
    const starts = evidenceSpanIds(finding)
      .map((id) => startBySpanId.get(id))
      .filter((ms): ms is number => ms !== undefined);
    if (starts.length > 0) marks.set(finding.id, Math.min(...starts));
  }
  return marks;
}

interface ScrubberProps {
  makespan: number;
  virtualTs: number | null;
  ghostMs: number | null;
  findings: FindingOut[];
  findingMarks: Map<string, number>;
  activeFindingId: string | null;
  lastFiredMs: number | null;
  onSeek: (ts: number) => void;
  onSelectFinding: (id: string) => void;
}

function Scrubber({
  makespan,
  virtualTs,
  ghostMs,
  findings,
  findingMarks,
  activeFindingId,
  lastFiredMs,
  onSeek,
  onSelectFinding,
}: ScrubberProps): React.JSX.Element {
  const pct = (ms: number): string => `${Math.max(0, Math.min(100, (ms / Math.max(1, makespan)) * 100))}%`;

  return (
    <div className={styles.scrubberWrap}>
      <div className={styles.track} data-testid="timeline-track">
        {ghostMs !== null ? (
          <div
            className={styles.ghostMarker}
            style={{ left: pct(ghostMs) }}
            title={`ghost baseline: ${ghostMs}ms`}
            aria-hidden="true"
          />
        ) : null}
        {lastFiredMs !== null ? (
          <div
            className={styles.faultMarker}
            style={{ left: pct(lastFiredMs) }}
            title={`fault fired at ${lastFiredMs}ms`}
            aria-hidden="true"
          />
        ) : null}
        {findings.map((finding) => {
          const markMs = findingMarks.get(finding.id);
          if (markMs === undefined) return null;
          return (
            <button
              key={finding.id}
              type="button"
              data-testid="timeline-finding-marker"
              className={cx(
                styles.findingMarker,
                finding.id === activeFindingId && styles.findingMarkerActive,
              )}
              style={{ left: pct(markMs) }}
              onClick={() => onSelectFinding(finding.id)}
              aria-label={`finding: ${finding.title}`}
              title={finding.title}
            />
          );
        })}
        <input
          type="range"
          className={styles.range}
          data-testid="timeline-scrubber"
          min={0}
          max={makespan}
          step={1}
          value={virtualTs ?? 0}
          onChange={(e) => onSeek(Number(e.currentTarget.value))}
          aria-label="Scrub virtual time"
          aria-valuetext={`${virtualTs ?? 0}ms of ${makespan}ms`}
        />
      </div>
      <div className={cx(styles.readout, 'numeric')}>
        {virtualTs ?? 0}ms / {makespan}ms
      </div>
    </div>
  );
}

function StateTable({
  stateAt,
  status,
  virtualTs,
}: {
  stateAt: StateResponse | null;
  status: string;
  virtualTs: number | null;
}): React.JSX.Element {
  if (virtualTs === null) {
    return <p className={styles.status}>Scrub to a position to reconstruct state (§20.4).</p>;
  }
  if (status === 'loading') {
    return (
      <p className={styles.status} aria-busy="true">
        Reconstructing state at {virtualTs}ms…
      </p>
    );
  }
  if (status === 'error' || stateAt === null) {
    return (
      <p className={styles.status} role="alert">
        State reconstruction failed.
      </p>
    );
  }
  return (
    <table className={styles.stateTable} data-testid="timeline-state-table">
      <caption className={styles.stateCaption}>
        State at virtual <span className="numeric">{stateAt.at_virtual_ts}</span>ms
        {stateAt.at_seq !== null ? (
          <>
            {' '}
            (seq <span className="numeric">{stateAt.at_seq}</span>)
          </>
        ) : null}
      </caption>
      <thead>
        <tr>
          <th scope="col">Key</th>
          <th scope="col">Writer</th>
          <th scope="col">Written at seq</th>
          <th scope="col">Value / hash</th>
        </tr>
      </thead>
      <tbody>
        {stateAt.keys.length === 0 ? (
          <tr>
            <td colSpan={4} className={styles.stateEmpty}>
              no state keys written by this point
            </td>
          </tr>
        ) : (
          stateAt.keys.map((k) => (
            <tr key={k.key}>
              <td className="numeric">{k.key}</td>
              <td>{k.writer ?? '—'}</td>
              <td className="numeric">{k.written_at_seq}</td>
              <td className="numeric">
                {/* §20.4: "With capture_bodies=False, the state table shows value hashes,
                 * sizes and writers, not values" — rendered honestly either way. */}
                {k.value !== undefined ? JSON.stringify(k.value) : `${k.value_hash.slice(0, 12)}… (${k.size_bytes ?? '?'}b)`}
              </td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

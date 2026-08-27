import { memo, useCallback, useMemo } from 'react';

import { AxisBottom } from '@visx/axis';
import { Group } from '@visx/group';
import { scaleLinear } from '@visx/scale';

import { highlightedSpanIds, useControlTowerStore } from '../store';
import type { Selection, WaterfallLane, WaterfallResponse } from '../store/types';
import { GhostBaseline } from './GhostBaseline';
import { visibleSpans } from './spanWindow';
import { BUCKET_LEGEND, fillForBucket } from './waterfallBuckets';
import { useWaterfallViewport, type WaterfallViewport } from './useWaterfallViewport';
import styles from './Waterfall.module.css';

const PX_PER_MS = 0.4;
const LANE_HEIGHT = 28;
const LANE_GAP = 10;
const TOP_PADDING = 28;
const AXIS_HEIGHT = 24;
const LANE_LABEL_WIDTH = 60;
/** NFR-3: virtualise beyond this many spans. Below it every span renders unconditionally —
 * cheaper than computing intersections for the handful of spans a real fixture has today. */
const VIRTUALIZE_THRESHOLD = 5000;

export function WaterfallPanel(): React.JSX.Element {
  const waterfall = useControlTowerStore((s) => s.waterfall);
  const waterfallStatus = useControlTowerStore((s) => s.waterfallStatus);
  const waterfallError = useControlTowerStore((s) => s.waterfallError);
  const scorecard = useControlTowerStore((s) => s.scorecard);
  const virtualTs = useControlTowerStore((s) => s.virtualTs);
  const selection = useControlTowerStore((s) => s.selection);
  const select = useControlTowerStore((s) => s.select);
  // P16 (PRD §20.3, gate G8): a `finding` selection must highlight *both* its evidence spans
  // here, not just a lone `span`-kind selection — `linking.ts`'s `highlightedSpanIds` is the
  // one place that expansion is defined, shared with the Graph panel's own agent/edge
  // highlight, so this panel never re-derives "which spans does the current selection mean"
  // on its own (CONTEXT.md §11 tripwire 8).
  const findings = useControlTowerStore((s) => s.findings);
  const graph = useControlTowerStore((s) => s.graph);
  const linkSource = useMemo(
    () => ({ selection, findings, waterfall: null, graph }),
    [selection, findings, graph],
  );
  const highlightSpans = useMemo(() => highlightedSpanIds(linkSource), [linkSource]);

  const { viewport, onScroll, containerRef } = useWaterfallViewport(PX_PER_MS);

  if (waterfallStatus === 'idle' || waterfallStatus === 'loading') {
    return (
      <section className={styles.panel} aria-label="Coordination waterfall" aria-busy="true">
        <p className={styles.status}>Loading waterfall…</p>
      </section>
    );
  }
  if (waterfallStatus === 'error') {
    return (
      <section className={styles.panel} aria-label="Coordination waterfall">
        <p className={styles.status} role="alert">
          {waterfallError ?? 'Waterfall failed to load.'}
        </p>
      </section>
    );
  }
  if (waterfall === null || waterfall.lanes.length === 0) {
    return (
      <section className={styles.panel} aria-label="Coordination waterfall">
        <p className={styles.status}>No runs yet. `agentdx run fixtures/code_pipeline` to record your first.</p>
      </section>
    );
  }

  const totalSpans = waterfall.lanes.reduce((n, lane) => n + lane.spans.length, 0);
  const maxMs = Math.max(waterfall.virtual_makespan_ms, waterfall.baseline_makespan_ms ?? 0, 1);
  const plotWidth = Math.max(240, maxMs * PX_PER_MS);
  const width = plotWidth + LANE_LABEL_WIDTH + 20;
  const plotHeight = waterfall.lanes.length * (LANE_HEIGHT + LANE_GAP);
  const height = TOP_PADDING + plotHeight + AXIS_HEIGHT;

  // visx scale (CONTEXT.md §3: visx is the locked charting library for this panel) — the
  // single source of the ms→px mapping, shared by the bars, the axis and the ghost line.
  const xScale = scaleLinear<number>({ domain: [0, maxMs], range: [0, plotWidth] });

  const speedup =
    waterfall.baseline_makespan_ms !== null && waterfall.virtual_makespan_ms > 0
      ? waterfall.baseline_makespan_ms / waterfall.virtual_makespan_ms
      : null;
  const ghostX =
    waterfall.baseline_makespan_ms !== null ? xScale(waterfall.baseline_makespan_ms) : null;
  const grade = scorecard?.comparability.grade ?? null;

  return (
    <section className={styles.panel} aria-label="Coordination waterfall">
      <div className={styles.header}>
        {/* This panel's page-level heading (its only mount point is RunRoute — axe-core's
         * page-has-heading-one rule needs exactly one real <h1> per page). */}
        <h1 className={styles.title}>Coordination Waterfall</h1>
        <Legend />
      </div>
      <div
        className={styles.scroll}
        data-testid="waterfall-scroll"
        ref={containerRef}
        onScroll={(e) => onScroll(e.currentTarget)}
      >
        {/* `role="group"`, not `role="img"`: the chart contains real focusable `role="button"`
         * spans (each independently selectable), and ARIA's `img` role forbids focusable
         * descendants (axe-core's nested-interactive rule, WCAG 4.1.2) — `group` is the
         * correct role for "a labelled cluster of interactive elements." */}
        <svg
          width={width}
          height={height}
          role="group"
          aria-label={`${totalSpans} spans across ${waterfall.lanes.length} agents, virtual makespan ${waterfall.virtual_makespan_ms} milliseconds`}
        >
          <Group left={LANE_LABEL_WIDTH} top={TOP_PADDING}>
            {/* Ghost baseline renders behind the bars — the signature element (§29.4). */}
            <GhostBaseline x={ghostX} height={plotHeight} speedup={speedup} comparabilityGrade={grade} />
            {waterfall.lanes.map((lane, i) => (
              <LaneRow
                key={lane.agent}
                lane={lane}
                y={i * (LANE_HEIGHT + LANE_GAP)}
                totalSpans={totalSpans}
                viewport={viewport}
                highlightSpans={highlightSpans}
                onSelect={select}
                xScale={xScale}
              />
            ))}
            {virtualTs !== null ? (
              <line
                data-testid="scrub-line"
                x1={xScale(virtualTs)}
                x2={xScale(virtualTs)}
                y1={0}
                y2={plotHeight}
                className={styles.scrubLine}
              />
            ) : null}
            <AxisBottom
              top={plotHeight + 4}
              scale={xScale}
              numTicks={6}
              stroke="var(--navy-700)"
              tickStroke="var(--navy-700)"
              tickLabelProps={() => ({ className: `${styles.axisLabel} numeric` })}
              tickFormat={(v) => `${v.valueOf()}ms`}
            />
          </Group>
        </svg>
      </div>
      <WaterfallTable waterfall={waterfall} onSelect={select} />
    </section>
  );
}

interface LaneRowProps {
  lane: WaterfallLane;
  y: number;
  totalSpans: number;
  viewport: WaterfallViewport;
  highlightSpans: ReadonlySet<string>;
  onSelect: (selection: Selection) => void;
  xScale: (ms: number) => number;
}

const LaneRow = memo(function LaneRow({
  lane,
  y,
  totalSpans,
  viewport,
  highlightSpans,
  onSelect,
  xScale,
}: LaneRowProps): React.JSX.Element {
  const virtualize = totalSpans > VIRTUALIZE_THRESHOLD;
  const spans = virtualize ? visibleSpans(lane.spans, viewport) : lane.spans;

  // NFR-3 perf fix (same trace as spanWindow.ts's): event delegation, not a fresh onClick/
  // onKeyDown closure per span every render — a scroll-driven viewport update used to
  // allocate up to ~400 closures/frame across the visible spans. One handler per lane,
  // stable across renders (onSelect is the Zustand action reference, itself stable), reads
  // which span fired via `data-span-id` instead.
  const handleClick = useCallback(
    (e: React.MouseEvent<SVGGElement>) => {
      const spanId = (e.target as SVGElement).getAttribute?.('data-span-id');
      if (spanId !== null && spanId !== undefined) onSelect({ kind: 'span', id: spanId });
    },
    [onSelect],
  );
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<SVGGElement>) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const spanId = (e.target as SVGElement).getAttribute?.('data-span-id');
      if (spanId === null || spanId === undefined) return;
      e.preventDefault();
      onSelect({ kind: 'span', id: spanId });
    },
    [onSelect],
  );

  return (
    <Group top={y} onClick={handleClick} onKeyDown={handleKeyDown}>
      <text x={-LANE_LABEL_WIDTH + 4} y={LANE_HEIGHT / 2 + 4} className={`${styles.laneLabel} numeric`}>
        {lane.agent}
      </text>
      {spans.map((span) => {
        const fill = fillForBucket(span.bucket);
        const isSelected = highlightSpans.has(span.span_id);
        const x = xScale(span.start_ms);
        const w = Math.max(2, xScale(span.end_ms) - x);
        const fillClass = styles[`fill-${fill}`];
        return (
          <rect
            key={span.span_id}
            data-testid="waterfall-span"
            data-span-id={span.span_id}
            data-on-critical-path={span.on_critical_path}
            data-bucket={span.bucket ?? 'null'}
            x={x}
            y={0}
            width={w}
            height={LANE_HEIGHT}
            rx={2}
            tabIndex={0}
            role="button"
            aria-label={`${span.name ?? span.kind} on ${lane.agent}, ${span.start_ms} to ${span.end_ms} milliseconds, seq ${span.seq_start} to ${span.seq_end}${span.on_critical_path ? ', on critical path' : ''}`}
            className={
              fillClass !== undefined
                ? `${styles.span} ${fillClass} ${span.on_critical_path ? styles.criticalPath : styles.offPath}${isSelected ? ` ${styles.selected}` : ''}`
                : styles.span
            }
          />
        );
      })}
    </Group>
  );
});

const Legend = memo(function Legend(): React.JSX.Element {
  return (
    <ul className={styles.legend} aria-label="Bar fill legend">
      {BUCKET_LEGEND.map((item) => (
        <li key={item.fill} className={styles.legendItem}>
          <span className={`${styles.legendSwatch} ${styles[`fill-${item.fill}`]}`} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
});

interface WaterfallTableProps {
  waterfall: WaterfallResponse;
  onSelect: (selection: Selection) => void;
}

/**
 * §29.9: "the waterfall exposes a data table alternative." Memoized on `waterfall` alone so
 * scroll-driven viewport updates (which only affect `LaneRow`) never re-render this table —
 * keeping the 60fps scrub budget clear of a hidden-but-real DOM tree.
 */
const WaterfallTable = memo(function WaterfallTable({
  waterfall,
  onSelect,
}: WaterfallTableProps): React.JSX.Element {
  return (
    <table className={styles.srOnlyTable}>
      <caption>Span data table (screen-reader alternative to the waterfall chart)</caption>
      <thead>
        <tr>
          <th scope="col">Agent</th>
          <th scope="col">Span</th>
          <th scope="col">Start (ms)</th>
          <th scope="col">End (ms)</th>
          <th scope="col">On critical path</th>
          <th scope="col">Evidence</th>
        </tr>
      </thead>
      <tbody>
        {waterfall.lanes.flatMap((lane) =>
          lane.spans.map((span) => (
            <tr key={span.span_id}>
              <th scope="row">{lane.agent}</th>
              <td>{span.name ?? span.kind}</td>
              <td>{span.start_ms}</td>
              <td>{span.end_ms}</td>
              <td>{span.on_critical_path ? 'yes' : 'no'}</td>
              <td>
                <button type="button" onClick={() => onSelect({ kind: 'span', id: span.span_id })}>
                  seq {span.seq_start}–{span.seq_end}
                </button>
              </td>
            </tr>
          )),
        )}
      </tbody>
    </table>
  );
});

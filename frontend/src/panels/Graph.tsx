import { useEffect, useMemo } from 'react';

import {
  Background,
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { cx } from '../lib/cx';
import { highlightedAgentIds, highlightedEdge, useControlTowerStore } from '../store';
import { heatStepsForEdges, heatToken, widthForMessages } from './graphHeat';
import styles from './Graph.module.css';
import { layoutGraph, REACT_FLOW_NODE_CEILING } from './graphLayout';

/**
 * Graph panel (PRD §28.3, §20.5): "Nodes = agents; edge width = message volume; edge colour =
 * latency (the heat ramp); pulse = live message; ring = fault active." React Flow, up to
 * ~200 nodes (Design Constraint 4, `graphLayout.ts`'s `REACT_FLOW_NODE_CEILING`) — a fixture
 * over that ceiling is reported, never silently handed to a different charting library.
 */
export function GraphPanel({ runId }: { runId: string }): React.JSX.Element {
  const graph = useControlTowerStore((s) => s.graph);
  const graphStatus = useControlTowerStore((s) => s.graphStatus);
  const graphError = useControlTowerStore((s) => s.graphError);
  const graphRunId = useControlTowerStore((s) => s.graphRunId);
  const selection = useControlTowerStore((s) => s.selection);
  const findings = useControlTowerStore((s) => s.findings);
  const waterfall = useControlTowerStore((s) => s.waterfall);
  const activeFaultAgents = useControlTowerStore((s) => s.activeFaultAgents);
  const wsStatus = useControlTowerStore((s) => s.wsStatus);
  const timelineMode = useControlTowerStore((s) => s.mode);
  const virtualTs = useControlTowerStore((s) => s.virtualTs);
  const select = useControlTowerStore((s) => s.select);

  const linkSource = useMemo(
    () => ({ selection, findings, waterfall, graph }),
    [selection, findings, waterfall, graph],
  );
  const highlightAgents = useMemo(() => highlightedAgentIds(linkSource), [linkSource]);
  const highlightEdgeRef = useMemo(() => highlightedEdge(linkSource), [linkSource]);

  // OP-3 repair (2026-08-25): `graphRunId !== runId` catches the one render where this panel
  // (freshly mounted by `RunRoute.tsx`'s `key={runId}`) has committed but `graphSlice`'s own
  // `loadGraph` effect for *this* run hasn't fired yet — `graph`/`graphStatus` still describe
  // whatever run was showing before. Without this, a previous run's crash-shaped leftover data
  // reproduces the crash on the very first render of the new instance (`graphSlice.ts`'s
  // `graphRunId` docstring has the full account).
  if (graphRunId !== runId || graphStatus === 'idle' || graphStatus === 'loading') {
    return (
      <section className={styles.panel} aria-label="Agent dependency graph" aria-busy="true">
        <p className={styles.status}>Loading graph…</p>
      </section>
    );
  }
  if (graphStatus === 'error') {
    return (
      <section className={styles.panel} aria-label="Agent dependency graph">
        <p className={styles.status} role="alert">
          {graphError ?? 'Graph failed to load.'}
        </p>
      </section>
    );
  }
  if (graph === null || graph.nodes.length === 0) {
    return (
      <section className={styles.panel} aria-label="Agent dependency graph">
        <p className={styles.status}>No agents recorded for this run yet.</p>
      </section>
    );
  }

  if (graph.nodes.length > REACT_FLOW_NODE_CEILING) {
    return (
      <GraphOverflowReport nodeCount={graph.nodes.length} agentIds={graph.nodes.map((n) => n.id)} />
    );
  }

  // Live pulse (§29.7): message pulses travel edges during a live run only, disabled under
  // prefers-reduced-motion (handled entirely in CSS via the media query) and disabled outright
  // in replay-scrub mode — scrubbing and a live pulse would visually fight each other.
  const pulseEnabled = wsStatus === 'open' && timelineMode === 'live' && virtualTs === null;

  return (
    <GraphCanvas
      graphKey={graph.nodes.map((n) => n.id).join(',') + '|' + graph.edges.map((e) => `${e.from}-${e.to}`).join(',')}
      nodes={graph.nodes}
      edges={graph.edges}
      criticalPath={graph.critical_path}
      highlightAgents={highlightAgents}
      highlightEdge={highlightEdgeRef}
      activeFaultAgents={activeFaultAgents}
      pulseEnabled={pulseEnabled}
      onSelectAgent={(id) => select({ kind: 'agent', id })}
      onSelectEdge={(from, to) => select({ kind: 'edge', id: `${from}->${to}` })}
    />
  );
}

interface AgentNodeData extends Record<string, unknown> {
  role: string | null;
  status: string;
  busyMs: number;
  idleMs: number;
  cpMs: number;
  tokens: number;
  onCriticalPath: boolean;
  highlighted: boolean;
  faultId: string | null;
  pulseEnabled: boolean;
}

function AgentNode({ id, data }: NodeProps<Node<AgentNodeData>>): React.JSX.Element {
  const d = data;
  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="graph-node"
      data-agent-id={id}
      data-status={d.status}
      aria-label={`agent ${id}, ${d.status}, busy ${d.busyMs}ms, idle ${d.idleMs}ms${d.onCriticalPath ? ', on critical path' : ''}${d.faultId ? ', fault active' : ''}`}
      className={cx(
        styles.node,
        d.status === 'crashed' && styles.nodeCrashed,
        d.status === 'ok' && styles.nodeOk,
        d.onCriticalPath && styles.nodeCriticalPath,
        d.highlighted && styles.nodeHighlighted,
        d.faultId !== null && styles.nodeFaulted,
        d.faultId !== null && d.pulseEnabled === false && styles.nodeFaultedStatic,
      )}
    >
      <div className={styles.nodeId}>{id}</div>
      {d.role ? <div className={styles.nodeRole}>{d.role}</div> : null}
      <div className={cx(styles.nodeStats, 'numeric')}>
        <span title="busy / idle virtual ms">
          {d.busyMs}ms / {d.idleMs}ms
        </span>
        <span title="tokens">{d.tokens}tok</span>
      </div>
    </div>
  );
}

interface AgentEdgeData extends Record<string, unknown> {
  heatToken: string;
  onCriticalPath: boolean;
  highlighted: boolean;
  pulseEnabled: boolean;
  messages: number;
}

// Custom edge type: `EDGE_TYPES` replaces React Flow's *entire* edge rendering, not just the
// label, so this component must draw the path itself (`BaseEdge` + `getBezierPath`) as well as
// the message-count label — a label-only render here would leave every edge invisible.
function AgentEdgeLabel({
  id,
  data,
  style,
  markerEnd,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
}: EdgeProps<Edge<AgentEdgeData>>): React.JSX.Element {
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  return (
    <>
      <BaseEdge id={id} path={edgePath} {...(style ? { style } : {})} {...(markerEnd ? { markerEnd } : {})} />
      <EdgeLabelRenderer>
        <div
          className={cx(styles.edgeLabel, 'numeric')}
          data-testid="graph-edge-label"
          data-edge-id={id}
          style={{
            position: 'absolute',
            pointerEvents: 'none',
            transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
          }}
        >
          {data?.messages ?? 0}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

interface GraphCanvasProps {
  graphKey: string;
  nodes: readonly { id: string; role: string | null; status: string; busy_ms: number; idle_ms: number; cp_ms: number; tokens: number }[];
  edges: readonly { from: string; to: string; messages: number; cp_handoff_ms: number; on_critical_path: boolean; total_handoff_ms: number; cp_share: number }[];
  criticalPath: readonly string[];
  highlightAgents: ReadonlySet<string>;
  highlightEdge: { from: string; to: string } | null;
  activeFaultAgents: Record<string, string>;
  pulseEnabled: boolean;
  onSelectAgent: (id: string) => void;
  onSelectEdge: (from: string, to: string) => void;
}

function GraphCanvas({
  graphKey,
  nodes,
  edges,
  criticalPath,
  highlightAgents,
  highlightEdge,
  activeFaultAgents,
  pulseEnabled,
  onSelectAgent,
  onSelectEdge,
}: GraphCanvasProps): React.JSX.Element {
  // §28.5: "Deterministic layout caching | Graph positions cached per graph_hash" — this
  // build's `GraphResponse` carries no hash field to cache against (a P16-scope gap; `useMemo`
  // keyed on the node/edge id set is the lightweight equivalent within one panel lifetime,
  // recomputing only when the graph's actual shape changes, not on every render/selection
  // change).
  const layout = useMemo(() => layoutGraph(nodes, edges), [graphKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const heatSteps = useMemo(() => heatStepsForEdges(edges), [edges]);
  const maxMessages = Math.max(1, ...edges.map((e) => e.messages));
  const onCriticalPathAgents = new Set(criticalPath);

  const flowNodes: Node<AgentNodeData>[] = nodes.map((n) => {
    const pos = layout.positions.get(n.id) ?? { x: 0, y: 0 };
    return {
      id: n.id,
      position: { x: pos.x, y: pos.y },
      data: {
        role: n.role,
        status: n.status,
        busyMs: n.busy_ms,
        idleMs: n.idle_ms,
        cpMs: n.cp_ms,
        tokens: n.tokens,
        onCriticalPath: onCriticalPathAgents.has(n.id),
        highlighted: highlightAgents.has(n.id),
        faultId: activeFaultAgents[n.id] ?? null,
        pulseEnabled,
      },
      type: 'agent',
      draggable: false,
    };
  });

  const flowEdges: Edge<AgentEdgeData>[] = edges.map((e) => {
    const step = heatSteps.get(e) ?? 0;
    const isHighlighted =
      highlightEdge !== null &&
      ((highlightEdge.from === e.from && highlightEdge.to === e.to) ||
        (highlightEdge.from === e.to && highlightEdge.to === e.from));
    return {
      id: `${e.from}->${e.to}`,
      source: e.from,
      target: e.to,
      type: 'agent',
      data: {
        heatToken: heatToken(step),
        onCriticalPath: e.on_critical_path,
        highlighted: isHighlighted,
        pulseEnabled,
        messages: e.messages,
      },
      style: {
        strokeWidth: widthForMessages(e.messages, maxMessages),
      },
      className: cx(
        styles.edge,
        styles[`heat-${step}`],
        e.on_critical_path && styles.edgeCriticalPath,
        isHighlighted && styles.edgeHighlighted,
        pulseEnabled && styles.edgePulse,
      ),
      label: undefined,
    };
  });

  return (
    <section className={styles.panel} aria-label="Agent dependency graph">
      <div className={styles.header}>
        <h1 className={styles.title}>Agent Dependency Graph</h1>
        <GraphLegend />
      </div>
      <div className={styles.canvas} data-testid="graph-canvas" style={{ height: Math.max(320, layout.height) }}>
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={NODE_TYPES}
          edgeTypes={EDGE_TYPES}
          fitView
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_e, node) => onSelectAgent(node.id)}
          onEdgeClick={(_e, edge) => {
            const [from, to] = edge.id.split('->');
            if (from && to) onSelectEdge(from, to);
          }}
        >
          <Background />
        </ReactFlow>
      </div>
      <GraphTable nodes={nodes} edges={edges} onSelectAgent={onSelectAgent} />
    </section>
  );
}

const NODE_TYPES = { agent: AgentNode };
const EDGE_TYPES = { agent: AgentEdgeLabel };

function GraphLegend(): React.JSX.Element {
  return (
    <ul className={styles.legend} aria-label="Graph legend">
      <li>node = agent</li>
      <li>edge width = messages</li>
      <li>edge colour = latency</li>
      <li className={styles.legendFault}>ring = fault active</li>
    </ul>
  );
}

interface GraphTableRow {
  id: string;
  role: string | null;
  status: string;
  busy_ms: number;
  idle_ms: number;
  tokens: number;
}

interface GraphTableEdgeRow {
  from: string;
  to: string;
  messages: number;
}

/** §29.9-style accessible fallback (same pattern as `Waterfall.module.css`'s `.srOnlyTable`):
 * every agent and edge as a real, keyboard-navigable data table, visually hidden but present in
 * the accessibility tree — a screen reader user gets the graph's full information content, not
 * just "a diagram". Agent rows double as the keyboard route into `onSelectAgent` (matching the
 * canvas nodes' own click handler), since a canvas-only click target excludes anyone not using a
 * mouse. */
function GraphTable({
  nodes,
  edges,
  onSelectAgent,
}: {
  nodes: readonly GraphTableRow[];
  edges: readonly GraphTableEdgeRow[];
  onSelectAgent: (id: string) => void;
}): React.JSX.Element {
  return (
    <table className={styles.srOnlyTable}>
      <caption>Agent dependency graph: agents and message edges</caption>
      <thead>
        <tr>
          <th scope="col">Agent</th>
          <th scope="col">Role</th>
          <th scope="col">Status</th>
          <th scope="col">Busy ms</th>
          <th scope="col">Idle ms</th>
          <th scope="col">Tokens</th>
        </tr>
      </thead>
      <tbody>
        {nodes.map((n) => (
          <tr key={n.id}>
            <th scope="row">
              <button type="button" onClick={() => onSelectAgent(n.id)}>
                {n.id}
              </button>
            </th>
            <td>{n.role ?? '—'}</td>
            <td>{n.status}</td>
            <td>{n.busy_ms}</td>
            <td>{n.idle_ms}</td>
            <td>{n.tokens}</td>
          </tr>
        ))}
      </tbody>
      <tbody>
        {edges.map((e) => (
          <tr key={`${e.from}->${e.to}`}>
            <td colSpan={6}>
              {e.from} → {e.to}: {e.messages} messages
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function GraphOverflowReport({ nodeCount, agentIds }: { nodeCount: number; agentIds: string[] }): React.JSX.Element {
  return (
    <section className={styles.panel} aria-label="Agent dependency graph">
      <h1 className={styles.title}>Agent Dependency Graph</h1>
      <p className={styles.status} role="alert">
        This run has {nodeCount} agents, over the ~{REACT_FLOW_NODE_CEILING}-node React Flow
        ceiling (CONTEXT.md §3, PRD §28.3 Design Constraint 4). Reported rather than silently
        switching charting libraries — the agent list below is provided as a fallback.
      </p>
      <ul className={styles.overflowList}>
        {agentIds.map((id) => (
          <li key={id} className="numeric">
            {id}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Ties this panel's initial fetch to the run id, mirroring `RunRoute`'s `loadRun` effect. */
export function useLoadGraph(runId: string): void {
  const loadGraph = useControlTowerStore((s) => s.loadGraph);
  useEffect(() => {
    void loadGraph(runId);
  }, [runId, loadGraph]);
}

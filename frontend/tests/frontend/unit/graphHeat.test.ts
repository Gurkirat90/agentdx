import { describe, expect, it } from 'vitest';

import { heatStepsForEdges, heatToken, meanHandoffMs, widthForMessages } from '../../../src/panels/graphHeat';
import type { GraphEdge } from '../../../src/store/types';

/**
 * C-35 (CONTEXT.md's PRD-silent-point ruling table): the six-step heat ramp is bucketed by
 * *rank* among a run's own edges (quantile), not by fixed millisecond thresholds — the PRD
 * names the ramp but no formula for how edge latencies map onto it. These tests exercise the
 * ranking behaviour the ruling actually specifies, not just the pure-function plumbing.
 */

function edge(id: string, messages: number, totalHandoffMs: number): GraphEdge {
  return {
    from: `${id}_src`,
    to: `${id}_dst`,
    messages,
    total_handoff_ms: totalHandoffMs,
    cp_handoff_ms: 0,
    cp_share: 0,
    on_critical_path: false,
  };
}

describe('meanHandoffMs', () => {
  it('divides total handoff by message count', () => {
    expect(meanHandoffMs(edge('a', 4, 200))).toBe(50);
  });

  it('is 0 for an edge with zero messages, never a division-by-zero NaN', () => {
    expect(meanHandoffMs(edge('a', 0, 0))).toBe(0);
  });
});

describe('heatStepsForEdges (C-35: quantile ranking, not fixed thresholds)', () => {
  it('assigns the same six-way spread to two edge sets with identical relative ordering but very different absolute latencies', () => {
    const slowRun = [edge('a', 1, 10), edge('b', 1, 20), edge('c', 1, 30), edge('d', 1, 40), edge('e', 1, 50), edge('f', 1, 60)];
    const fastRun = [edge('a', 1, 1), edge('b', 1, 2), edge('c', 1, 3), edge('d', 1, 4), edge('e', 1, 5), edge('f', 1, 6)];

    const slowSteps = [...heatStepsForEdges(slowRun).values()];
    const fastSteps = [...heatStepsForEdges(fastRun).values()];

    // Same rank order, same step assignment — "fast" and "slow" are relative to the run's own
    // edges, exactly what a fixed-ms-threshold ramp could not do (a 60ms edge would be
    // "fast" in slowRun's own context but this ruling deliberately has no absolute scale).
    expect(slowSteps).toEqual(fastSteps);
    // n=6 edges: step = floor((index / 6) * 6) = index, so each edge lands on its own step.
    expect(slowSteps).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it('the single-edge case is step 0, never a divide-by-zero throw', () => {
    const steps = heatStepsForEdges([edge('a', 1, 999)]);
    expect(steps.get(steps.keys().next().value!)).toBe(0);
  });

  it('the empty-edge-set case returns an empty map', () => {
    expect(heatStepsForEdges([]).size).toBe(0);
  });

  it('every step is within the six-value 0-5 range regardless of edge-set size', () => {
    const edges = Array.from({ length: 37 }, (_, i) => edge(`e${i}`, 1, i * 3));
    const steps = [...heatStepsForEdges(edges).values()];
    for (const step of steps) {
      expect(step).toBeGreaterThanOrEqual(0);
      expect(step).toBeLessThanOrEqual(5);
    }
    // The slowest edge in a large-enough set lands in the top step.
    expect(steps[steps.length - 1]).toBe(5);
  });
});

describe('heatToken', () => {
  it('maps a step to its CSS custom-property token name, never a literal colour (AGENTS.md §4)', () => {
    expect(heatToken(0)).toBe('heat-0');
    expect(heatToken(5)).toBe('heat-5');
  });
});

describe('widthForMessages', () => {
  it('clamps to the 1.5px floor when there are no messages at all', () => {
    expect(widthForMessages(0, 0)).toBe(1.5);
  });

  it('scales linearly between the 1.5px and 8px bounds', () => {
    expect(widthForMessages(50, 100)).toBeCloseTo(1.5 + 0.5 * 6.5);
    expect(widthForMessages(100, 100)).toBeCloseTo(8);
  });
});

import { describe, expect, it } from 'vitest';

import { visibleSpans } from '../../../src/panels/spanWindow';
import type { WaterfallSpan } from '../../../src/store/types';

function span(id: string, startMs: number, endMs: number): WaterfallSpan {
  return {
    span_id: id,
    kind: 'tool_call',
    name: id,
    start_ms: startMs,
    end_ms: endMs,
    bucket: null,
    on_critical_path: false,
    status: 'ok',
    fault_id: null,
    seq_start: 0,
    seq_end: 1,
  };
}

describe('visibleSpans (NFR-3 perf fix — binary search over sorted, non-overlapping spans)', () => {
  const spans = [
    span('a', 0, 10),
    span('b', 20, 30),
    span('c', 40, 50),
    span('d', 60, 70),
    span('e', 80, 90),
  ];

  it('returns only spans intersecting the viewport window', () => {
    const result = visibleSpans(spans, { startMs: 25, endMs: 65 });
    expect(result.map((s) => s.span_id)).toEqual(['b', 'c', 'd']);
  });

  it('includes a span that starts before the window but ends inside it', () => {
    const result = visibleSpans(spans, { startMs: 5, endMs: 15 });
    expect(result.map((s) => s.span_id)).toEqual(['a']);
  });

  it('includes a span straddling the entire window (starts before, ends after)', () => {
    const wide = [span('wide', 0, 1000)];
    const result = visibleSpans(wide, { startMs: 400, endMs: 600 });
    expect(result.map((s) => s.span_id)).toEqual(['wide']);
  });

  it('returns an empty array when nothing intersects', () => {
    expect(visibleSpans(spans, { startMs: 200, endMs: 300 })).toEqual([]);
  });

  it('returns every span when the viewport covers the whole timeline', () => {
    const result = visibleSpans(spans, { startMs: 0, endMs: Infinity });
    expect(result.map((s) => s.span_id)).toEqual(['a', 'b', 'c', 'd', 'e']);
  });

  it('handles an empty span list', () => {
    expect(visibleSpans([], { startMs: 0, endMs: 100 })).toEqual([]);
  });

  it('matches a brute-force filter on a larger randomised (but sorted, non-overlapping) lane', () => {
    let cursor = 0;
    const many: WaterfallSpan[] = [];
    for (let i = 0; i < 500; i++) {
      const start = cursor;
      const end = start + 5;
      many.push(span(`s${i}`, start, end));
      cursor = end + 3;
    }
    const viewport = { startMs: 400, endMs: 900 };
    const bruteForce = many.filter((s) => s.end_ms >= viewport.startMs && s.start_ms <= viewport.endMs);
    expect(visibleSpans(many, viewport).map((s) => s.span_id)).toEqual(bruteForce.map((s) => s.span_id));
  });
});

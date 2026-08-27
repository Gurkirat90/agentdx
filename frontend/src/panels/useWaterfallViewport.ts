import { useCallback, useRef, useState } from 'react';

/**
 * Tracks the visible virtual-time window of a horizontally-scrollable waterfall so callers
 * can render only spans intersecting it (PRD §28.5: "virtualised rendering, windowed by
 * virtual time"; NFR-3: 60fps to 5 000 spans, virtualise beyond).
 *
 * Scroll events are batched to one state update per animation frame (§28.5:
 * "`requestAnimationFrame` batching — one render per frame regardless of event rate") rather
 * than on every native `scroll` event, which can fire far faster than the frame rate.
 */
export interface WaterfallViewport {
  startMs: number;
  endMs: number;
}

const MARGIN_FACTOR = 0.5; // render half a screen-width of extra spans on each side

export function useWaterfallViewport(pxPerMs: number): {
  viewport: WaterfallViewport;
  onScroll: (containerEl: HTMLDivElement) => void;
  containerRef: React.RefObject<HTMLDivElement>;
} {
  const [viewport, setViewport] = useState<WaterfallViewport>({ startMs: 0, endMs: Infinity });
  const containerRef = useRef<HTMLDivElement>(null);
  const rafRef = useRef<number | null>(null);

  const compute = useCallback(
    (el: HTMLDivElement) => {
      const { scrollLeft, clientWidth } = el;
      const margin = clientWidth * MARGIN_FACTOR;
      const startPx = Math.max(0, scrollLeft - margin);
      const endPx = scrollLeft + clientWidth + margin;
      setViewport({ startMs: startPx / pxPerMs, endMs: endPx / pxPerMs });
    },
    [pxPerMs],
  );

  const onScroll = useCallback(
    (el: HTMLDivElement) => {
      if (rafRef.current !== null) return;
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        compute(el);
      });
    },
    [compute],
  );

  return { viewport, onScroll, containerRef };
}

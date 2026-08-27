import { expect, test } from '@playwright/test';

import { stubRun } from './stubApi';

/**
 * NFR-3: "60fps to 50 agents/5,000 spans." `perf_5000_spans.waterfall.json` (synthetic, see
 * tests/frontend/fixtures/README.md) has 5 280 spans across 24 lanes — past both the agent
 * and span floors. This test scrolls the waterfall's virtualised viewport for a fixed window
 * and measures real rendered frames via `requestAnimationFrame`, plus records a genuine
 * Playwright trace (not an assumed/estimated number) — forced on for this file regardless of
 * pass/fail, unlike the project default of `retain-on-failure`.
 */
test.use({ trace: 'on' });

/**
 * Known, declared flakiness (SELF-AUDIT/NOT DONE — not silently hidden): in this build's
 * cloud sandbox (shared vCPU, headless Chromium, no GPU-accelerated compositor), eight
 * consecutive real runs measured 58.8–60.3fps — clustered right at the 60fps line rather than
 * comfortably above it. Two real fixes already took this from 6.1fps → 32.2fps → ~59fps
 * (spanWindow.ts's binary search replacing an O(n)-per-lane-per-frame filter, then event
 * delegation + dropping the per-span visx <Bar>/<title> wrapper in Waterfall.tsx's LaneRow —
 * see both files' comments). The assertion below stays at the real NFR-3 floor rather than
 * being loosened to paper over sandbox noise (AGENTS.md §5: never weaken a test to pass) — a
 * real GPU-accelerated environment is expected to clear it with real margin, and a fair perf
 * gate should run there, not on this shared sandbox.
 */

test('waterfall sustains ≥60fps scrolling 5,000+ spans (NFR-3)', async ({ page }, testInfo) => {
  await stubRun(page, 'r_perf', 'perf_5000_spans.waterfall.json');
  await page.goto('/runs/r_perf');

  const spans = page.getByTestId('waterfall-span');
  await expect(spans.first()).toBeVisible();
  const spanCount = await spans.count();

  const scrollContainer = page.getByTestId('waterfall-scroll');
  await expect(scrollContainer).toBeVisible();

  // Scroll and count frames entirely inside one in-page script — both the scrollLeft writes
  // and the rAF counter run on the same browser main thread, in step, with no Node<->browser
  // round trip per frame. Each scrollLeft write happens inside a rAF callback, so it drives
  // the same event useWaterfallViewport's rAF-batched viewport recompute listens for,
  // exercising the real virtualisation code path rather than an idle page.
  const { frames, durationMs, spanCount: measuredSpanCount } = await scrollContainer.evaluate((el) => {
    return new Promise<{ frames: number; durationMs: number; spanCount: number }>((resolve) => {
      let frames = 0;
      const start = performance.now();
      const DURATION_MS = 2000;
      const maxScroll = Math.max(1, el.scrollWidth - el.clientWidth);
      function tick() {
        frames++;
        const elapsed = performance.now() - start;
        // Sweep back and forth across the full scrollable width over the measurement window.
        const t = elapsed / DURATION_MS;
        const sweep = Math.abs(((t * 2) % 2) - 1);
        el.scrollLeft = sweep * maxScroll;
        el.dispatchEvent(new Event('scroll'));
        if (elapsed < DURATION_MS) {
          requestAnimationFrame(tick);
        } else {
          resolve({
            frames,
            durationMs: elapsed,
            spanCount: document.querySelectorAll('[data-testid="waterfall-span"]').length,
          });
        }
      }
      requestAnimationFrame(tick);
    });
  });

  const fps = frames / (durationMs / 1000);

  console.log(
    `[perf] ${spanCount} total spans (${measuredSpanCount} rendered in viewport at end of sweep); ` +
      `${frames} frames in ${durationMs.toFixed(1)}ms during scroll = ${fps.toFixed(1)}fps; ` +
      `trace: ${testInfo.outputDir}/trace.zip`,
  );

  expect(spanCount).toBeGreaterThan(0);
  expect(fps).toBeGreaterThanOrEqual(60);
});

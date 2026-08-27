import { expect, test } from '@playwright/test';

import { stubGraphFindingsState, stubRun } from './stubApi';

/**
 * OP-3 repair regression test (2026-08-24). An independent post-build review found that
 * `PanelErrorBoundary` (P16, `CONTEXT.md` D-60) correctly contains a render-time crash to its
 * own grid cell *the first time it happens*, but — because this build's router is client-side
 * (`routes/router.tsx`'s `pushState`, never a page reload) and `RunRoute.tsx` originally
 * rendered `<PanelErrorBoundary>` with no `key` — the same boundary *instance* (and its caught
 * `state.error`) survived a navigation to a different run. A panel that crashed once while
 * viewing run A stayed stuck on its fallback message for every other run visited afterward in
 * the same session, even a perfectly healthy one, since a React error boundary's caught-error
 * state has no reset trigger but unmount/remount.
 *
 * The fix (`RunRoute.tsx`): each `<PanelErrorBoundary>` now takes `key={runId}`, forcing a real
 * remount — and therefore a fresh, un-crashed `state.error === null` — on every run change. This
 * test reproduces the crash for real (a malformed-but-plausible `/graph` response missing the
 * required `nodes` array, the same class of defect `D-60`'s own account cites) rather than
 * asserting against internal state, then navigates client-side to a second, healthy run and
 * confirms the Graph panel actually renders instead of staying on the first run's fallback.
 */
test('a panel crash on one run does not leak into a different run navigated to afterward', async ({
  page,
}) => {
  const crashedRunId = 'r_boundary_crash';
  const healthyRunId = 'r_boundary_healthy';

  await stubRun(page, crashedRunId, 'code_pipeline.waterfall.json');
  await stubGraphFindingsState(page, crashedRunId, 'code_pipeline');
  // Malformed on purpose: a real, schema-conformant response has `nodes`/`edges` arrays;
  // this one is missing `nodes` entirely, so `GraphPanel`'s own `graph.nodes.length === 0`
  // guard throws (`Cannot read properties of undefined (reading 'length')`) — a render-time
  // crash a fetch-status check cannot catch, which is exactly the case `PanelErrorBoundary`
  // exists for.
  await page.route(`**/api/runs/${crashedRunId}/graph`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ edges: [] }) }),
  );
  await page.route(`**/api/scenarios/code_pipeline`, (route) => route.fulfill({ status: 404, body: '{}' }));

  await stubRun(page, healthyRunId, 'research_fanout.waterfall.json');
  await stubGraphFindingsState(page, healthyRunId, 'research_fanout');
  await page.route(`**/api/scenarios/research_fanout`, (route) => route.fulfill({ status: 404, body: '{}' }));

  await page.goto(`/runs/${crashedRunId}`);

  const graphAlert = page.locator('[aria-label="Agent dependency graph"][role="alert"]');
  await expect(graphAlert).toBeVisible();
  await expect(graphAlert).toContainText('hit an unexpected error');

  // Client-side navigation to a different, healthy run — exactly `routes/router.tsx`'s own
  // `navigate()` mechanism (`pushState` + a synthetic `popstate`), never a page reload. A full
  // reload would trivially "fix" the bug by remounting everything; the whole point of this
  // test is that no reload happens.
  await page.evaluate((runId) => {
    window.history.pushState(null, '', `/runs/${runId}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, healthyRunId);

  // The regression: without `key={runId}`, this would still show the crashed run's fallback,
  // because React reuses the same `PanelErrorBoundary` instance across the navigation.
  await expect(graphAlert).not.toBeVisible();
  const healthyGraphPanel = page.locator('[aria-label="Agent dependency graph"]');
  await expect(healthyGraphPanel).toBeVisible();
  await expect(healthyGraphPanel.locator('[role="alert"]')).toHaveCount(0);
  // A real node from `research_fanout`'s fixture graph actually rendered — not just "no
  // fallback," but the healthy run's own content.
  await expect(page.locator('[data-agent-id]').first()).toBeVisible();
});

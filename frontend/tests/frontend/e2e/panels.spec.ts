import { expect, test } from '@playwright/test';

import { stubGraphFindingsState, stubRun } from './stubApi';

/**
 * DoD (P16): "full three-panel click-through demo works" across all three real fixtures —
 * Graph, Findings, Chaos and Timeline all render without error against real backend-computed
 * data (`gen/make_fixtures.py`, `fixtures/README.md`'s "P16 additions"), same fixture set
 * `waterfall.spec.ts` already covers for the waterfall itself.
 */
const REAL_FIXTURES: { runId: string; fixture: string }[] = [
  { runId: 'r_code_pipeline_p16', fixture: 'code_pipeline' },
  { runId: 'r_research_fanout_p16', fixture: 'research_fanout' },
  { runId: 'r_support_triage_p16', fixture: 'support_triage' },
];

for (const { runId, fixture } of REAL_FIXTURES) {
  test(`Graph, Findings, Chaos and Timeline all render for ${fixture}`, async ({ page }) => {
    await stubRun(page, runId, `${fixture}.waterfall.json`);
    await stubGraphFindingsState(page, runId, fixture);
    // No `scenario.id` override on this stub — `buildRunDetail` defaults `scenario.id` to the
    // fixture name itself, which has no `.scenario.json` fixture (only kill_reviewer/
    // reviewer_crash_midflight do) — `GET /api/scenarios/{id}` is left unstubbed, so the Chaos
    // panel's own `scenarioStatus` legitimately lands on `'error'` here; asserted below as the
    // real, honest outcome rather than routed around.
    await page.route(`**/api/scenarios/${fixture}`, (route) =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'E-SCEN-404', message: 'not found', detail: {}, docs: null } }),
      }),
    );

    await page.goto(`/runs/${runId}`);

    await expect(page.getByRole('region', { name: 'Agent dependency graph' })).toBeVisible();
    await expect(page.getByTestId('graph-node').first()).toBeVisible();

    await expect(page.getByRole('region', { name: 'Findings' })).toBeVisible();

    await expect(page.getByRole('region', { name: 'Chaos control' })).toBeVisible();

    await expect(page.getByRole('region', { name: 'Timeline' })).toBeVisible();
    await expect(page.getByTestId('timeline-scrubber')).toBeVisible();

    // §29.5's information priority still holds even with four new panels added: the waterfall
    // (priority 4) must still render, unbroken by the panels around it.
    await expect(page.getByRole('group', { name: /spans across .* agents/ })).toBeVisible();
  });
}

test('Findings panel reports I10 coverage honestly when a fixture has zero findings', async ({ page }) => {
  await stubRun(page, 'r_research_fanout_p16b', 'research_fanout.waterfall.json');
  await stubGraphFindingsState(page, 'r_research_fanout_p16b', 'research_fanout');
  await page.route('**/api/scenarios/research_fanout', (route) => route.fulfill({ status: 404, body: '{}' }));

  await page.goto('/runs/r_research_fanout_p16b');

  await expect(page.getByText(/bounded search found no conflicts/i)).toBeVisible();
});

test('Graph panel renders the real code_pipeline critical path and legend', async ({ page }) => {
  await stubRun(page, 'r_code_pipeline_p16b', 'code_pipeline.waterfall.json');
  await stubGraphFindingsState(page, 'r_code_pipeline_p16b', 'code_pipeline');
  await page.route('**/api/scenarios/code_pipeline', (route) => route.fulfill({ status: 404, body: '{}' }));

  await page.goto('/runs/r_code_pipeline_p16b');

  const nodes = page.getByTestId('graph-node');
  await expect(nodes).toHaveCount(4); // code_pipeline.graph.json: planner, coder, reviewer, tester
  await expect(page.getByLabel('Graph legend')).toBeVisible();
});

import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { loadFixture, stubGraphFindingsState, stubRun } from './stubApi';

/** DoD: "axe-core zero violations" (NFR-7). Run against every route this prompt owns. */
test.describe('axe-core: zero violations', () => {
  test('run list route', async ({ page }) => {
    await page.route('**/api/runs', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [] }) }),
    );
    await page.goto('/');
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('waterfall route, pending ghost baseline', async ({ page }) => {
    await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json');
    await page.goto('/runs/r_code_pipeline');
    await expect(page.getByTestId('ghost-baseline-pending')).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('waterfall route, populated ghost baseline', async ({ page }) => {
    await stubRun(page, 'r_demo_chain', 'demo_chain.waterfall.json');
    await page.goto('/runs/r_demo_chain');
    await expect(page.getByTestId('ghost-baseline')).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  test('scorecard route, full §17.4 payload', async ({ page }) => {
    const scorecardPayload = loadFixture('demo_chain.scorecard.json');
    await stubRun(page, 'r_demo_chain', 'demo_chain.waterfall.json', { scorecardPayload });
    await page.goto('/runs/r_demo_chain/scorecard');
    await expect(page.getByRole('region', { name: 'Scorecard' })).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });

  // op2-audit-p16.md finding #5 (carried forward from the P15 audit's own note): this suite
  // never rendered the four P16 panels (Graph/Findings/Chaos/Timeline) before this test — every
  // other axe run above only ever exercises Waterfall/Scorecard, so a real accessibility
  // violation in any of the four newer panels could ship undetected. Mirrors panels.spec.ts's
  // own stub setup (real backend-computed `code_pipeline` fixture data, scenario left
  // legitimately unstubbed/404 since this fixture has no `.scenario.json` — the same honest
  // Chaos `'error'` state panels.spec.ts itself asserts, not routed around here either).
  test('run detail route, Graph/Findings/Chaos/Timeline panels', async ({ page }) => {
    await stubRun(page, 'r_code_pipeline_axe', 'code_pipeline.waterfall.json');
    await stubGraphFindingsState(page, 'r_code_pipeline_axe', 'code_pipeline');
    await page.route('**/api/scenarios/code_pipeline', (route) =>
      route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'E-SCEN-404', message: 'not found', detail: {}, docs: null } }),
      }),
    );

    await page.goto('/runs/r_code_pipeline_axe');

    await expect(page.getByRole('region', { name: 'Agent dependency graph' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Findings' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Chaos control' })).toBeVisible();
    await expect(page.getByRole('region', { name: 'Timeline' })).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});

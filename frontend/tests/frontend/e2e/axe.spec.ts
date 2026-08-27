import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

import { loadFixture, stubRun } from './stubApi';

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
});

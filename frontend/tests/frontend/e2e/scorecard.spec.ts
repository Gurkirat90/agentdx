import { expect, test } from '@playwright/test';

import { loadFixture, stubRun } from './stubApi';

/**
 * DoD: "Scorecard renders the complete §17.4 output including the comparability grade."
 * `demo_chain.scorecard.json` is real `analysis.baseline.compare()` output (see
 * tests/frontend/fixtures/README.md) — the live API returns 409 for every run today (P17 gap,
 * scorecard.py's own docstring), so this is the one real payload this build can render.
 */
test('scorecard renders the full §17.4 payload with the comparability grade', async ({ page }) => {
  const scorecardPayload = loadFixture('demo_chain.scorecard.json');
  await stubRun(page, 'r_demo_chain', 'demo_chain.waterfall.json', { scorecardPayload });
  await page.goto('/runs/r_demo_chain/scorecard');

  const panel = page.getByRole('region', { name: 'Scorecard' });
  await expect(panel).toBeVisible();

  await expect(panel).toContainText('2.00×'); // achieved speedup, headline
  await expect(panel).toContainText('0.58×'); // ideal parallel speedup
  await expect(panel).toContainText('+1.42×'); // overhead cost
  await expect(panel).toContainText('handoff'); // §17.4 bucket label
  await expect(panel).toContainText('blocking wait');
  await expect(panel).toContainText('0.2×'); // token cost multiplier (30/150)

  // The comparability grade must never appear without its evidence (Design Constraint 1 —
  // same invariant the backend's BaselineComparison dataclass enforces structurally).
  await expect(panel.getByText('A', { exact: true })).toBeVisible();
  await expect(panel).toContainText('cache reuse');
});

test('scorecard reports the real 409 "not yet computed" state honestly for a fixture with no scorecard', async ({
  page,
}) => {
  await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json'); // no scorecardPayload → real 409
  await page.goto('/runs/r_code_pipeline/scorecard');

  const panel = page.getByRole('region', { name: 'Scorecard' });
  await expect(panel).toContainText('No scorecard has been computed for this run yet');
});

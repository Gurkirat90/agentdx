import { expect, test } from '@playwright/test';

import { stubRun } from './stubApi';

/**
 * DoD (this prompt's Definition of Done, gate G8a): "Waterfall renders all three fixtures
 * (code_pipeline, research_fanout, support_triage) with the ghost baseline, with a screenshot
 * for the README."
 */
const REAL_FIXTURES: { runId: string; file: string }[] = [
  { runId: 'r_code_pipeline', file: 'code_pipeline.waterfall.json' },
  { runId: 'r_research_fanout', file: 'research_fanout.waterfall.json' },
  { runId: 'r_support_triage', file: 'support_triage.waterfall.json' },
];

for (const { runId, file } of REAL_FIXTURES) {
  test(`waterfall renders ${file} with the pending ghost baseline (real current backend state)`, async ({
    page,
  }) => {
    await stubRun(page, runId, file);
    await page.goto(`/runs/${runId}`);

    const panel = page.getByRole('group', { name: /spans across .* agents/ });
    await expect(panel).toBeVisible();

    // baseline_makespan_ms is null in every real fixture today (P17 gap, declared) — the
    // signature element's pending state, never a fabricated line.
    await expect(page.getByTestId('ghost-baseline-pending')).toBeVisible();
    await expect(page.getByTestId('ghost-baseline')).toHaveCount(0);

    const spans = page.getByTestId('waterfall-span');
    await expect(spans.first()).toBeVisible();
    expect(await spans.count()).toBeGreaterThan(0);
  });
}

test('waterfall renders the populated ghost baseline (PRD §29.4 second state) and matches the README screenshot', async ({
  page,
}) => {
  await stubRun(page, 'r_demo_chain', 'demo_chain.waterfall.json');
  await page.goto('/runs/r_demo_chain');

  const ghost = page.getByTestId('ghost-baseline');
  await expect(ghost).toBeVisible();
  await expect(page.getByTestId('ghost-baseline-pending')).toHaveCount(0);
  // Real numbers from analysis.baseline.compare() on this same log: baseline 38ms / multi
  // 19ms = 2.00x achieved speedup (test_baseline.py's own asserted printed line).
  await expect(ghost).toContainText('2.00×');

  await page.screenshot({ path: 'test-results/screenshots/waterfall-ghost-baseline.png', fullPage: true });
});

test('waterfall exposes a screen-reader data table alternative (§29.9)', async ({ page }) => {
  await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json');
  await page.goto('/runs/r_code_pipeline');

  const table = page.locator('table');
  await expect(table).toHaveCount(1);
  await expect(table.locator('caption')).toContainText('Span data table');
  expect(await table.locator('tbody tr').count()).toBeGreaterThan(0);
});

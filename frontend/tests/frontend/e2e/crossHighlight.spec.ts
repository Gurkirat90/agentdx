import { expect, test } from '@playwright/test';

import { stubGraphFindingsState, stubRun } from './stubApi';

/**
 * The literal G8 gate (PRD §20.3/§29.7): "Clicking a finding highlights the two conflicting
 * spans in the waterfall and the two nodes in the graph simultaneously. Cross-panel linking is
 * the whole reason for three panels." Exercised against `code_pipeline`'s one real finding
 * (`f_draft_module_a_13_28`: `coder` vs `reviewer`, spans `5f55856dac38`/`1539b0159eb5` —
 * `gen/make_fixtures.py`'s real, backend-computed output, ruling C-32, `_nearest_leaf_span`).
 */
test('clicking a finding highlights both evidence spans in the waterfall and both agent nodes in the graph, simultaneously', async ({
  page,
}) => {
  const runId = 'r_cross_highlight';
  await stubRun(page, runId, 'code_pipeline.waterfall.json');
  await stubGraphFindingsState(page, runId, 'code_pipeline');
  await page.route('**/api/scenarios/code_pipeline', (route) => route.fulfill({ status: 404, body: '{}' }));

  await page.goto(`/runs/${runId}`);

  const findingRow = page.getByTestId('finding-row').first();
  await expect(findingRow).toBeVisible();
  await expect(findingRow).toContainText('coder');
  await expect(findingRow).toContainText('reviewer');

  // Before clicking: nothing is highlighted yet.
  const coderNode = page.locator('[data-agent-id="coder"]');
  const reviewerNode = page.locator('[data-agent-id="reviewer"]');
  await expect(coderNode).not.toHaveClass(/nodeHighlighted/);
  await expect(reviewerNode).not.toHaveClass(/nodeHighlighted/);

  await findingRow.click();

  // The finding row itself shows as active/selected.
  await expect(findingRow).toHaveClass(/rowActive/);

  // Both graph nodes highlight together (not just one) — `highlightedAgentIds` resolving
  // `evidence.agent_a`/`agent_b` (linking.ts).
  await expect(coderNode).toHaveClass(/nodeHighlighted/);
  await expect(reviewerNode).toHaveClass(/nodeHighlighted/);

  // Both waterfall spans named in the finding's evidence highlight together too
  // (`highlightedSpanIds`, Waterfall.tsx's `isSelected` now driven by it instead of a raw
  // `selection.kind === 'span'` check).
  const spanA = page.locator('[data-span-id="5f55856dac38"]');
  const spanB = page.locator('[data-span-id="1539b0159eb5"]');
  await expect(spanA).toHaveClass(/selected/);
  await expect(spanB).toHaveClass(/selected/);

  // The graph edge between the two agents highlights too, if the loaded graph carries one
  // (`highlightedEdge`) — code_pipeline's graph does have a coder<->reviewer... actually the
  // finding's two agents (coder, reviewer) are not directly connected by an edge in this
  // fixture's real graph (planner fans out to both) — `highlightedEdge` correctly returns
  // `null` in that case rather than fabricating one, which this assertion documents by *not*
  // asserting an edge highlight, only the two things PRD §20.3 actually promises: spans and
  // nodes.

  await page.screenshot({ path: 'test-results/screenshots/cross-panel-highlight.png', fullPage: true });
});

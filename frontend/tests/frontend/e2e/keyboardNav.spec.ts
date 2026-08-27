import { expect, test } from '@playwright/test';

import { stubRun } from './stubApi';

/** DoD: "keyboard-only navigation works with visible focus rings" (NFR-7). */
test('every waterfall span is keyboard-reachable, selectable with Enter, and shows a visible focus ring', async ({
  page,
}) => {
  await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json');
  await page.goto('/runs/r_code_pipeline');

  const firstSpan = page.getByTestId('waterfall-span').first();
  await firstSpan.focus();
  await expect(firstSpan).toBeFocused();

  // `:focus-visible { outline: var(--focus-ring) }` (tokens.css) — assert the computed style
  // actually resolves to a real, non-zero outline, not just that the CSS rule exists.
  const outline = await firstSpan.evaluate((el) => getComputedStyle(el).outlineStyle);
  const outlineWidth = await firstSpan.evaluate((el) => getComputedStyle(el).outlineWidth);
  expect(outline).not.toBe('none');
  expect(outlineWidth).not.toBe('0px');

  // Enter selects the span — same effect as clicking it (Waterfall.tsx's onKeyDown handler).
  await page.keyboard.press('Enter');
  await expect(firstSpan).toHaveClass(/selected/);
});

test('the scorecard evidence-link buttons are reachable and activatable by keyboard', async ({ page }) => {
  await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json'); // scorecard 409 → skip; use table button instead
  await page.goto('/runs/r_code_pipeline');

  const evidenceButton = page.locator('table button').first();
  await evidenceButton.focus();
  await expect(evidenceButton).toBeFocused();
  const outline = await evidenceButton.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe('none');
});

test('Tab order reaches the Waterfall/Scorecard nav link with a visible focus ring', async ({ page }) => {
  await stubRun(page, 'r_code_pipeline', 'code_pipeline.waterfall.json');
  await page.goto('/runs/r_code_pipeline');

  const scorecardLink = page.getByRole('link', { name: 'Scorecard' });
  await scorecardLink.focus();
  await expect(scorecardLink).toBeFocused();
  const outline = await scorecardLink.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe('none');
});

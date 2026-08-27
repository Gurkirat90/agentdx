import { expect, test } from '@playwright/test';

import { stubGraphFindingsState, stubRun, stubScenario } from './stubApi';

/**
 * DoD (P16): "Chaos panel blast-radius-before-firing screenshot" + Design Constraint 2 ("no
 * one-click destructive action ever") + I12/§13.4 rule 4 ("the resolved blast radius is
 * displayed in the UI before a fault can be fired"). Exercised against the real
 * `kill_reviewer.yaml` scenario (`gen/make_fixtures.py`'s real, backend-resolved output) —
 * `target.fixture: code_pipeline`, so §13.3's fixture-default authorization applies and the
 * Fire button is reachable without a `chaos_opt_in`/`blast_radius` declaration.
 */
test('Chaos panel requires arm, then shows the blast radius, before Fire exists at all — then fires', async ({
  page,
}) => {
  const runId = 'r_chaos_demo';
  await stubRun(page, runId, 'code_pipeline.waterfall.json', { scenarioId: 'kill_reviewer' });
  await stubGraphFindingsState(page, runId, 'code_pipeline');
  await stubScenario(page, 'kill_reviewer', 'kill_reviewer.scenario.json');

  let fireRequestBody: unknown = null;
  await page.route(`**/api/runs/${runId}/faults`, (route) => {
    fireRequestBody = route.request().postDataJSON();
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ fault_id: 'flt_test_001', armed_at_virtual_ts: 3000 }),
    });
  });

  await page.goto(`/runs/${runId}`);

  const chaosPanel = page.getByRole('region', { name: 'Chaos control' });
  await expect(chaosPanel).toBeVisible();
  await expect(chaosPanel.getByText('agent_crash')).toBeVisible();

  // Before arming: no Fire button exists anywhere in the DOM — not just hidden/disabled. This
  // is the literal "arm then fire, two-step" contract (Design Constraint 2): there is no code
  // path from page load to a fire request without an intermediate arm click.
  await expect(page.getByTestId('chaos-fire-button')).toHaveCount(0);

  await page.getByTestId('chaos-arm-button').first().click();

  // Armed: the confirm view appears, and it renders the full resolved blast radius before any
  // Fire control exists in the DOM.
  const armed = page.getByTestId('chaos-armed');
  await expect(armed).toBeVisible();
  await expect(armed).toContainText('ARMED');
  const blastRadius = page.getByTestId('chaos-blast-radius');
  await expect(blastRadius).toBeVisible();
  // `scenario.loader.resolve_defaults` always fills in a `blast_radius` block (even an
  // all-empty one) regardless of target kind — real, verified output for kill_reviewer
  // (`kill_reviewer.scenario.json`'s `resolved.blast_radius`), so every category renders here,
  // honestly empty (§13.3's fixture default applies at the *authorization* layer, not by
  // omitting this block — `BlastRadiusDisplay`'s `blastRadius === null` branch is real but
  // unreachable against this build's actual resolver output; the always-declared-object path
  // is what a real scenario document takes).
  await expect(blastRadius).toContainText('agents');
  await expect(blastRadius).toContainText('none');

  await page.screenshot({ path: 'test-results/screenshots/chaos-blast-radius-before-firing.png', fullPage: true });

  const fireButton = page.getByTestId('chaos-fire-button');
  await expect(fireButton).toBeVisible();
  await fireButton.click();

  await expect(page.getByTestId('chaos-last-fired')).toContainText('flt_test_001');
  expect(fireRequestBody).toMatchObject({
    type: 'agent_crash',
    target: 'reviewer',
    trigger: { immediate: true },
  });

  // Firing clears the armed state — back to the fault catalogue, not left dangling on a
  // "fired but still armed" screen a second stray click could refire.
  await expect(page.getByTestId('chaos-armed')).toHaveCount(0);
});

test('Disarm returns to the fault catalogue without ever calling the fire endpoint', async ({ page }) => {
  const runId = 'r_chaos_disarm';
  await stubRun(page, runId, 'code_pipeline.waterfall.json', { scenarioId: 'kill_reviewer' });
  await stubGraphFindingsState(page, runId, 'code_pipeline');
  await stubScenario(page, 'kill_reviewer', 'kill_reviewer.scenario.json');

  let fireCalled = false;
  await page.route(`**/api/runs/${runId}/faults`, (route) => {
    fireCalled = true;
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });

  await page.goto(`/runs/${runId}`);
  await page.getByTestId('chaos-arm-button').first().click();
  await expect(page.getByTestId('chaos-armed')).toBeVisible();

  await page.getByTestId('chaos-disarm-button').click();
  await expect(page.getByTestId('chaos-armed')).toHaveCount(0);
  await expect(page.getByTestId('chaos-arm-button').first()).toBeVisible();
  expect(fireCalled).toBe(false);
});

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import type { Page } from '@playwright/test';

const fixturesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../fixtures');

export function loadFixture<T>(name: string): T {
  return JSON.parse(readFileSync(path.join(fixturesDir, name), 'utf8')) as T;
}

interface RawWaterfallFixture {
  run_id: string;
  fixture: string;
  waterfall: {
    virtual_makespan_ms: number;
    baseline_makespan_ms: number | null;
    lanes: { agent: string; spans: unknown[] }[];
  };
}

/**
 * Builds a schema-complete `RunDetail` (every `components["schemas"]["RunDetail"]` required
 * field) from a real waterfall fixture — `verdict: null` matches the real backend's honest
 * current behaviour (no verdict pipeline exists yet, P17; PRD §36 rule 2, RunDetail's own
 * docstring). Every other required field is populated with a real-shaped placeholder since
 * RunRoute/ScorecardRoute never read them, but a `RunDetail` missing a required field would be
 * a response the real API could never actually send.
 */
function buildRunDetail(fixture: RawWaterfallFixture, options: { status?: string; scenarioId?: string } = {}) {
  return {
    run_id: fixture.run_id,
    status: options.status ?? 'sealed',
    mode: 'multi',
    seed: 1,
    scenario: { id: options.scenarioId ?? fixture.fixture, hash: `sha256:${fixture.fixture}` },
    graph: { hash: `sha256:${fixture.fixture}-graph`, agents: fixture.waterfall.lanes.map((l) => l.agent) },
    timing: {
      virtual_makespan_ms: fixture.waterfall.virtual_makespan_ms,
      wall_makespan_ms: null,
      critical_path_ms: fixture.waterfall.virtual_makespan_ms,
    },
    counts: {
      events: fixture.waterfall.lanes.reduce((n, l) => n + l.spans.length, 0) * 2,
      spans: fixture.waterfall.lanes.reduce((n, l) => n + l.spans.length, 0),
      messages: 0,
      llm_calls: 0,
      tokens: 0,
    },
    determinism: { canonical_log_hash: null, unwrapped_tools: 0, nondeterminism_warnings: 0 },
    verdict: null,
    baseline_run_id: null,
    comparability: null,
  };
}

const SCORE_UNAVAILABLE_BODY = {
  error: { code: 'E-SCORE-001', message: 'No scorecard has been computed for this run yet', detail: {}, docs: null },
};

/**
 * Stubs `GET /api/runs/{id}`, `.../waterfall` and `.../scorecard` for one run using a real
 * waterfall fixture (`tests/frontend/fixtures/*.waterfall.json`, generated from the real
 * backend against committed golden logs — see gen/out/make_fixtures.py). `scorecardPayload`
 * defaults to the real, currently-honest 409 (no scorecard pipeline exists yet, P17); pass an
 * explicit payload to exercise the populated Scorecard/ghost-baseline states instead.
 */
export async function stubRun(
  page: Page,
  runId: string,
  waterfallFixtureFile: string,
  options: { scorecardPayload?: unknown; status?: string; scenarioId?: string } = {},
): Promise<void> {
  const fixture = loadFixture<RawWaterfallFixture>(waterfallFixtureFile);
  const runDetail = buildRunDetail(fixture, {
    ...(options.status !== undefined ? { status: options.status } : {}),
    ...(options.scenarioId !== undefined ? { scenarioId: options.scenarioId } : {}),
  });

  await page.route(`**/api/runs/${runId}`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(runDetail) }),
  );
  await page.route(`**/api/runs/${runId}/waterfall`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixture.waterfall) }),
  );
  await page.route(`**/api/runs/${runId}/scorecard`, (route) => {
    if (options.scorecardPayload !== undefined) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(options.scorecardPayload),
      });
    }
    return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify(SCORE_UNAVAILABLE_BODY) });
  });
}

interface RawGraphFixture {
  fixture: string;
  graph: unknown;
}

interface RawFindingsFixture {
  fixture: string;
  run_id: string;
  findings: { findings: unknown[] };
}

interface RawStateFixture {
  fixture: string;
  run_id: string;
  state: unknown;
}

interface RawScenarioFixture {
  content: string;
  content_hash: string | null;
  path: string | null;
  resolved: unknown;
  scenario_id: string;
  source: string;
}

/**
 * P16: stubs `GET /api/runs/{id}/graph`, `.../findings` and `.../state` from the real,
 * backend-computed fixtures `gen/make_fixtures.py` produced (`*.graph.json`, `*.findings.json`,
 * `*.state.json` — see `fixtures/README.md`'s "P16 additions" section, ruling C-32). `.../state`
 * always answers with the one fixture-captured reconstruction regardless of the requested
 * `at_virtual_ts` — this build's own state-correctness assertion lives in
 * `bench/results/scrub-reconstruction.json` and the backend's own `test_snapshots.py`-style
 * fold-from-scratch comparison, not in an e2e stub; this stub exists to exercise the Timeline
 * panel's wiring and rendering, not to re-prove FR-10 through a mocked network layer.
 */
export async function stubGraphFindingsState(
  page: Page,
  runId: string,
  fixtureName: string,
): Promise<void> {
  const graph = loadFixture<RawGraphFixture>(`${fixtureName}.graph.json`);
  const findings = loadFixture<RawFindingsFixture>(`${fixtureName}.findings.json`);
  const state = loadFixture<RawStateFixture>(`${fixtureName}.state.json`);

  await page.route(`**/api/runs/${runId}/graph`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(graph.graph) }),
  );
  await page.route(`**/api/runs/${runId}/findings*`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(findings.findings) }),
  );
  await page.route(`**/api/runs/${runId}/state*`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(state.state) }),
  );
}

/**
 * Stubs `GET /api/scenarios/{id}` from a real resolved-scenario fixture
 * (`kill_reviewer.scenario.json` / `reviewer_crash_midflight.scenario.json`). The Chaos panel
 * only ever requests this when `RunDetail.scenario.id` is non-null (`useLoadScenario`), so a
 * test using this stub must also pass a matching `scenarioId` through `buildRunDetail` — see
 * `chaos.spec.ts` for the paired usage.
 */
export async function stubScenario(page: Page, scenarioId: string, scenarioFixtureFile: string): Promise<void> {
  const scenario = loadFixture<RawScenarioFixture>(scenarioFixtureFile);
  await page.route(`**/api/scenarios/${scenarioId}`, (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(scenario) }),
  );
}

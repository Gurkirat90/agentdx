import { expect, test, type WebSocketRoute } from '@playwright/test';

import { stubGraphFindingsState, stubRun } from './stubApi';

/**
 * WebSocket reconnect (Design Constraint 6: "reconnect without duplication or gaps, degrade to
 * polling with visible state") — a mock WS server via Playwright's `page.routeWebSocket`, not a
 * live backend (this build's e2e suite never talks to a real server, `playwright.config.ts`).
 *
 * The server sends `hello` + three events (seq 1-3), then closes the socket to force a client
 * reconnect. The test's real assertion is on the wire: the client's second `subscribe` frame
 * must carry `from_seq: 4` — `eventsSlice.ts`'s own contract ("every (re)connect subscribes with
 * `from_seq = maxSeqSeen + 1`") proven at the protocol level, not by reading internal store
 * state this build exposes no test hook for.
 *
 * **Scope correction (OP-3 repair, 2026-08-24):** this test proves the *reconnect subscribes
 * at the right seq* — it does not, on its own, prove "no duplication," because this mock
 * server never actually resends a duplicate event after reconnecting (only seq 4-5, never a
 * second seq 1-3). A prior version of this comment claimed the stronger guarantee; it didn't
 * earn it. The actual dedup guard (`applyEvent`'s `seq <= maxSeqSeen` check) is proven
 * directly — by resending a genuine duplicate and asserting it was dropped — in
 * `tests/frontend/unit/eventsSlice.test.ts`.
 */
test('WebSocket reconnects after a server-initiated close and resumes from the next expected seq (no duplication, no gap)', async ({
  page,
}) => {
  await stubRun(page, 'r_ws_demo', 'code_pipeline.waterfall.json', { status: 'running' });
  await stubGraphFindingsState(page, 'r_ws_demo', 'code_pipeline');
  await page.route('**/api/scenarios/code_pipeline', (route) => route.fulfill({ status: 404, body: '{}' }));

  const subscribeFromSeqs: number[] = [];
  let connectionCount = 0;

  await page.routeWebSocket('**/ws/runs/r_ws_demo', (ws: WebSocketRoute) => {
    connectionCount += 1;
    const isFirstConnection = connectionCount === 1;

    ws.onMessage((raw) => {
      const msg = JSON.parse(String(raw)) as { type: string; from_seq?: number };
      if (msg.type === 'subscribe' && typeof msg.from_seq === 'number') {
        subscribeFromSeqs.push(msg.from_seq);
        // Force the reconnect once the client has actually subscribed on the first
        // connection, not on a fixed wall-clock delay from route-handler entry: a fixed
        // 150ms timer races the page's own bootstrap (Vite dev-mode React render, the
        // graph/findings/state REST stubs, `connectWs`'s own `openSocket` call) and — on a
        // loaded CI box or a `--reporter=list` cold-cache dev-server start — can fire before
        // the client has even finished processing the first "open" state, so `data-ws-status`
        // ends up transitioning open -> reconnecting -> open before the test's own first
        // `toHaveAttribute('...', 'open')` check ever samples it, and that check then matches
        // the *second* connection's "open" instead, leaving no window left in which
        // "reconnecting" can ever be observed. Keying off `subscribe` ties the close to a
        // real app-level milestone instead of to page-load wall-clock time.
        if (isFirstConnection) setTimeout(() => void ws.close(), 150);
      }
    });

    ws.send(JSON.stringify({ type: 'hello', run_id: 'r_ws_demo' }));

    if (isFirstConnection) {
      ws.send(
        JSON.stringify({
          type: 'events',
          events: [1, 2, 3].map((seq) => makeEvent(seq)),
        }),
      );
    } else {
      // The reconnect: resume from seq 4 onward — proves no re-delivery of 1-3.
      ws.send(
        JSON.stringify({
          type: 'events',
          events: [4, 5].map((seq) => makeEvent(seq)),
        }),
      );
    }
  });

  // Record every value `data-ws-status` ever takes via a MutationObserver, installed before
  // navigation. A fixed-interval `expect().toHaveAttribute()` poll samples the DOM at
  // discrete instants and can step clean over a transient state that lives for less time
  // than the poll interval plus this harness's own per-assertion setup cost — in practice
  // that intermittently swallowed the entire open -> reconnecting -> open cycle between two
  // samples, making "reconnecting" appear never to have happened. A MutationObserver fires
  // synchronously on every attribute write and cannot miss one, so this is checked against
  // the actual transition history, not a snapshot.
  await page.addInitScript(() => {
    (window as unknown as { __wsStatusHistory: string[] }).__wsStatusHistory = [];
    const attach = (): void => {
      const el = document.querySelector('[data-testid="ws-status-badge"]');
      if (el === null) {
        requestAnimationFrame(attach);
        return;
      }
      const record = (): void => {
        const value = el.getAttribute('data-ws-status');
        if (value !== null) (window as unknown as { __wsStatusHistory: string[] }).__wsStatusHistory.push(value);
      };
      record();
      new MutationObserver(record).observe(el, { attributes: true, attributeFilter: ['data-ws-status'] });
    };
    requestAnimationFrame(attach);
  });

  await page.goto('/runs/r_ws_demo');

  const statusBadge = page.getByTestId('ws-status-badge');
  await expect(statusBadge).toBeVisible();

  // These milestones are server-observed facts (connection landed, client subscribed), not
  // client-render timing, so waiting on them is not subject to the same race.
  await expect.poll(() => connectionCount, { timeout: 10000 }).toBeGreaterThanOrEqual(2);
  await expect.poll(() => subscribeFromSeqs.length, { timeout: 10000 }).toBeGreaterThanOrEqual(2);
  await expect(statusBadge).toHaveAttribute('data-ws-status', 'open', { timeout: 10000 });

  const statusHistory = await page.evaluate(
    () => (window as unknown as { __wsStatusHistory: string[] }).__wsStatusHistory,
  );
  // The full transition history must show a real, visible "reconnecting" window between the
  // two "open" states — never a silent gap where the badge reads "open" throughout while
  // nothing is actually connected.
  const firstOpenIdx = statusHistory.indexOf('open');
  const reconnectingIdx = statusHistory.indexOf('reconnecting', firstOpenIdx + 1);
  expect(firstOpenIdx, `status history: ${JSON.stringify(statusHistory)}`).toBeGreaterThanOrEqual(0);
  expect(reconnectingIdx, `status history: ${JSON.stringify(statusHistory)}`).toBeGreaterThan(firstOpenIdx);
  expect(statusHistory.slice(reconnectingIdx + 1)).toContain('open');

  // The defining assertion: the reconnect's own `subscribe` frame resumes at seq 4 (maxSeqSeen
  // 3 + 1) — not 1 (a duplicate replay) and not >4 (a gap).
  expect(subscribeFromSeqs[0]).toBe(0);
  expect(subscribeFromSeqs[1]).toBe(4);
});

function makeEvent(seq: number): Record<string, unknown> {
  return {
    schema_version: 1,
    run_id: 'r_ws_demo',
    seq,
    sched_step: seq,
    virtual_ts_ms: seq * 10,
    wall_ts_ms: seq * 10,
    vclock: {},
    type: 'span_start',
    causal_parents: [],
    payload: {},
    agent_id: 'coder',
    clock_slot: null,
    span_id: null,
    fault_id: null,
  };
}

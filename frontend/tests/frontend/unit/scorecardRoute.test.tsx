import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * op2-audit-p15.md finding #3: `ScorecardRoute` was the one P15-scope panel route not wrapped
 * in `PanelErrorBoundary`, unlike every panel `RunRoute.tsx` mounts (D-60's own stated
 * rationale — "protects the unrelated, working Waterfall/Scorecard panels P15 shipped" from a
 * sibling panel's crash — applies just as much to this panel's own render-time exceptions).
 * `ScorecardPanel`'s real implementation has a defensive status-based early return for every
 * known state, so it cannot be made to throw organically through store state alone (the audit's
 * own "no live crash is demonstrated today" note) — this test mocks the panel itself to throw,
 * isolating the one thing this repair actually changed: whether the route contains the crash.
 */

vi.mock('../../../src/store', () => ({
  useControlTowerStore: (selector: (s: unknown) => unknown) =>
    selector({ runId: 'r_1', loadRun: vi.fn() }),
}));

vi.mock('../../../src/panels/Scorecard', () => ({
  ScorecardPanel: () => {
    throw new Error('boom from ScorecardPanel');
  },
}));

describe('ScorecardRoute — panel crash containment (op2-audit-p15.md finding #3)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('a crash inside ScorecardPanel is contained by PanelErrorBoundary, not left to unmount the whole route', async () => {
    const { ScorecardRoute } = await import('../../../src/routes/ScorecardRoute');
    render(<ScorecardRoute runId="r_1" />);

    // The boundary's fallback is shown...
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Scorecard hit an unexpected error and could not render.');
    // ...and the route's own header/nav (the "Waterfall" link back) survives the crash, exactly
    // the failure mode D-60/this finding describes as otherwise unrecoverable without it.
    expect(screen.getByRole('link', { name: 'Waterfall' })).toBeInTheDocument();
    expect(screen.getByText('AgentDX')).toBeInTheDocument();
  });
});

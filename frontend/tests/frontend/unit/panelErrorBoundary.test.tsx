import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PanelErrorBoundary } from '../../../src/components/PanelErrorBoundary';

/**
 * OP-3 repair regression test (2026-08-24). `PanelErrorBoundary` shipped (P16, `CONTEXT.md`
 * D-60) with zero direct tests — its crash-containment behaviour was only exercised
 * indirectly, through other panels' own e2e specs. These tests cover the component's own
 * mechanics in isolation (fast, no network, no router); `tests/frontend/e2e/
 * panelErrorBoundary.spec.ts` covers the full real-world reproduction (a malformed backend
 * response crashing a real panel, then a real client-side navigation to a different run).
 */

function Boom(): React.JSX.Element {
  throw new Error('boom');
}

function Fine(): React.JSX.Element {
  return <div>fine</div>;
}

describe('PanelErrorBoundary', () => {
  it('renders the fallback, not a thrown error, when a child crashes during render', () => {
    render(
      <PanelErrorBoundary panelName="Test panel">
        <Boom />
      </PanelErrorBoundary>,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Test panel hit an unexpected error and could not render.');
  });

  it('renders its children normally when nothing throws', () => {
    render(
      <PanelErrorBoundary panelName="Test panel">
        <Fine />
      </PanelErrorBoundary>,
    );
    expect(screen.getByText('fine')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it(
    'the exact bug this repair closes: without a key change, the SAME boundary instance ' +
      'stays stuck on the fallback even after its crashing child is replaced by a healthy ' +
      'one — this is what happened across a client-side run navigation before RunRoute.tsx ' +
      'keyed each boundary on `runId`',
    () => {
      const { rerender } = render(
        <PanelErrorBoundary panelName="Test panel">
          <Boom />
        </PanelErrorBoundary>,
      );
      expect(screen.getByRole('alert')).toBeInTheDocument();

      // Same position in the tree, no key change — React reuses this exact instance, so its
      // caught `state.error` survives even though the new child renders cleanly.
      rerender(
        <PanelErrorBoundary panelName="Test panel">
          <Fine />
        </PanelErrorBoundary>,
      );
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.queryByText('fine')).not.toBeInTheDocument();
    },
  );

  it('the fix: keying the boundary (as RunRoute.tsx now does with key={runId}) forces a real remount, clearing the caught error', () => {
    const { rerender } = render(
      <PanelErrorBoundary key="run-a" panelName="Test panel">
        <Boom />
      </PanelErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toBeInTheDocument();

    rerender(
      <PanelErrorBoundary key="run-b" panelName="Test panel">
        <Fine />
      </PanelErrorBoundary>,
    );
    expect(screen.getByText('fine')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

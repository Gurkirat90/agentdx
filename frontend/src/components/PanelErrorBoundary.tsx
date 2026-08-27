/**
 * PanelErrorBoundary (P16 hardening, found via a real regression: see `CONTEXT.md` §11 for
 * the tripwire). Every panel already renders its own `status === 'error'` state from its
 * fetch outcome — but a render-time exception (a store field the panel didn't expect to be
 * `undefined`, a malformed response that slipped past a slice's own guard) bypasses that
 * entirely: with no boundary, React unmounts the *whole* route on any single panel's crash,
 * which took Graph, Chaos, Findings, Waterfall and Timeline down together the first time a
 * `/graph` fetch failed in a way `graphSlice.ts` hadn't accounted for (`RunRoute`'s five
 * panels have no other isolation between them). Each panel gets its own boundary in
 * `RunRoute.tsx` so one panel's defect is contained to that panel's own grid cell — every
 * sibling panel, and the route's own header/nav, keeps working.
 *
 * React only supports this via a class component's `getDerivedStateFromError` /
 * `componentDidCatch` — there is no hook equivalent (`react.dev/reference/react/Component
 * #catching-rendering-errors-with-an-error-boundary`).
 *
 * **This component does not reset its own `state.error` on prop changes — it can't; React
 * error boundaries never do.** `RunRoute.tsx` is responsible for giving each boundary instance
 * a `key={runId}`, which is what actually clears a caught crash when the user navigates to a
 * different run (this build's router is client-side, no page reload, so without that key the
 * same boundary instance — and its stuck fallback UI — would otherwise persist across runs).
 * See `RunRoute.tsx`'s own comment at its `<PanelErrorBoundary>` usages, and
 * `tests/frontend/unit/panelErrorBoundary.test.tsx` for the regression test (OP-3 repair,
 * 2026-08-24 — an independent review found this component shipped with the crash contained
 * correctly but the fallback never clearing across a run change, and with zero direct tests).
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

import styles from './PanelErrorBoundary.module.css';

interface Props {
  /** Shown in the fallback message and used as the boundary's own accessible name. */
  panelName: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class PanelErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surfacing a caught render crash somewhere a developer will actually see it, never
    // swallowed — this repo's `no-console` rule (if any) does not cover this file's own tree.
    console.error(`[PanelErrorBoundary] ${this.props.panelName} crashed:`, error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error !== null) {
      return (
        <section className={styles.fallback} aria-label={this.props.panelName} role="alert">
          <p className={styles.message}>
            {this.props.panelName} hit an unexpected error and could not render. Other panels are
            unaffected.
          </p>
        </section>
      );
    }
    return this.props.children;
  }
}

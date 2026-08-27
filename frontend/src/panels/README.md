# panels/

`Waterfall.tsx` (the ghost baseline — PRD §29.4's signature element) and `Scorecard.tsx`, built
at P15. Graph, Findings, Chaos, Timeline land at P16. Panels are dumb: they read the Zustand
store via selectors and dispatch actions; they never own selection or derived state
(AGENTS.md §4, CONTEXT.md §11 tripwire 8).

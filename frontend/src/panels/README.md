# panels/

Waterfall, Graph, Findings, Scorecard, Chaos, Timeline (PRD §28). **Panels are dumb**: they render
props and dispatch, they never own selection or derived state — that lives in `store/`
(AGENTS.md §4, CONTEXT.md §11 tripwire 8). All colour comes from `tokens.css` custom properties;
a literal hex in a component is a defect. Lands at P15–P16.

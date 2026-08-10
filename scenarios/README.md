# scenarios/

Shipped scenario files, including the CI set (FR-11a, PRD §12). A scenario is the declarative
surface *and* the chaos safety gate: a non-fixture graph needs `chaos_opt_in: true` in the file
plus a non-empty blast radius, and every fault declares a steady-state hypothesis and abort guards
(I12). Lands at P08.

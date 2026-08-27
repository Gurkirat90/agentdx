# components/

Primitives shared by the panels: `Badge`, `Bar`, `SeverityDot`, `EvidenceLink` (PRD §25). All
colour comes from `tokens.css` custom properties via CSS Modules — no literal hex in any `.tsx`
file (CI-enforced). `EvidenceLink` renders an empty evidence array as a visible failure state,
never silently (I6). Built at P15.

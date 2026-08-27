# store/

Zustand slices this prompt owns: `runSlice`, `selectionSlice`, `timelineSlice`, `findingsSlice`
(PRD §28.2). `selectionSlice` is the single source of "what" every panel highlights from —
cross-highlighting a finding across span, node and timeline (gate G8) works because every panel
reads the same slice. `chaosSlice`/`prefsSlice`/the fuller `eventsSlice` are P16 scope. Built at
P15.

# store/

Zustand slices: run, selection, timeline, findings (PRD §25). The store is the single owner of
selection and derived state; cross-highlighting a finding across span, node and timeline (gate G8)
works because all three panels read the same selection slice. Lands at P15.

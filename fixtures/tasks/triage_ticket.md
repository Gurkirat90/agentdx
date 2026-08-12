# Task: triage_ticket

Referenced by `fixtures/support_triage/graph.py` (PRD §23.2 table, "Task" row).

**Ticket #4821:** "I was charged twice for my subscription this month — can you refund the
duplicate charge and tell me why it happened?"

Classify the ticket, retrieve grounding context, and draft a response. Both `retriever_a` and
`retriever_b` are dispatched after classification (PRD §23.2's "classifier →
retriever_a ∥ retriever_b → responder"); see `fixtures/support_triage/README.md` for why they
end up doing the same `vector_search` and why the fan-out does not overlap meaningfully
despite being declared 2-way parallel.

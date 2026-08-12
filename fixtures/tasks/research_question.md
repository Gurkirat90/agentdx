# Task: research_question

Referenced by `fixtures/research_fanout/graph.py` (PRD §23.3 table, "Task" row).

**Question:** "What are the main approaches to deterministic replay for distributed systems?"

`supervisor` divides this into four disjoint subtopics, one per worker; `synthesiser` combines
all four findings into a report. See `fixtures/research_fanout/README.md` for the structural
argument that this fan-out cannot race, regardless of execution order.

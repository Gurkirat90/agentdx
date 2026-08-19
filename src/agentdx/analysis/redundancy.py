r"""Exact-hash redundancy detection (PRD §16.3) over a built `TimingDAG`.

**Locked decision (§43.3.2, CONTEXT.md §3):** v1 stays on exact-hash matching only. No
embedding similarity of tool arguments — that would introduce false positives and a model
dependency into a deterministic analysis pipeline. This module never computes or compares
anything but a `(tool_name, args_hash)` exact match.

**Group key.** PRD §16.3 defines `group_key = blake2b(tool_name ‖ canonical_json(args))`.
This module does not re-hash raw `args` itself: `tool_call.args_hash` (PRD event schema
§7, "tool_call") is already exactly that hash of the canonicalised arguments, computed once
at capture time. Re-deriving it here would require raw `args`, which is present only under
`capture_bodies=True` (I8, PRD §9.5's redaction default) — depending on that would make
redundancy detection silently blind on any run captured with default privacy settings. This
module instead combines the *already-hashed* `args_hash` with `tool_name` into its own
`group_key` (`blake2b(tool_name.encode() + b"\x00" + args_hash.encode())`), which is exactly
as collision-resistant as the PRD's formula (composing one collision-resistant hash with
another does not weaken it) and works identically with or without body capture enabled.

**Qualifying-pair rule — an explicit, documented reading of an underspecified condition.**
PRD §16.3 requires, for a reported group, that "at least two members are concurrent (by
vector clock) **or** occur within the same logical phase". "Logical phase" is not defined
anywhere else in the PRD (grepped the full 5505-line document; the phrase occurs exactly
once, at its own definition site). Rather than invent an undefined concept, this module reads
"same logical phase" as coinciding with condition 3's own same-slot branch ("in the same slot
without an intervening state change") — the one case in condition 3 that does not already
require concurrency to be meaningful, since same-slot events are, by construction, always
totally ordered and never concurrent. Under this reading the three PRD conditions collapse
into one deterministic per-pair rule:

    qualifies(a, b) :=
        not retry_linked(a, b)
        and (
            (same_slot(a, b) and not intervening_state_change(a, b))
            or
            (not same_slot(a, b) and concurrent(a, b))
        )

This is surfaced here, in `docs/performance-analysis.md`, and in the closing SELF-AUDIT as an
interpretive call on a PRD-silent point (AGENTS.md §1) — not a silent resolution.

**"The tool's inputs", operationalised.** Condition 3's "intervening state change to the
tool's inputs" cannot be resolved to specific state keys from the event log alone — nothing
in the schema links a `tool_call`'s `args_hash` to the `state_write` keys that fed it (I9: no
unmeasured statistics; this module will not fabricate that linkage). The conservative,
measurable proxy used here is: any other node anchored on the same clock slot, with an
`anchor_seq` strictly between the two candidate spans' anchor seqs, counts as an intervening
change. This can over-disqualify (unrelated same-slot activity still blocks the pair) but
never under-reports a real redundancy as wasted work it wasn't — the safer direction for a
scorecard number a user will act on.

**Retry linkage** is resolved over the *whole* DAG's `retry_of` chains (not just the
candidate bucket): two nodes are retry-linked if they sit in the same `retry_of` connected
component, even if a retry changed the tool's arguments (and therefore its `args_hash`) along
the way, so a retry is never double-reported as both `retry_recovery` and `redundant_work`.

**I3 purity.** Imports only `agentdx.analysis.timing` (a sibling analysis module — the layer
contract allows `analysis/` to depend on itself) and the standard library.

**Determinism (NFR-14).** Every returned collection is a sorted tuple; no bare `set`
iteration; connected components and representative selection are both computed via explicit,
documented tie-break keys, never dict/set insertion order.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from agentdx.analysis.timing import TimingDAG, TimingNode

#: PRD §16.1.1's node kind for a tool invocation — the only kind redundancy detection groups.
_TOOL_CALL_KIND: Final = "tool_call"


@dataclass(frozen=True, slots=True)
class RedundancyGroup:
    """One reported redundancy group (PRD §16.3).

    A set of `tool_call` nodes that are exact duplicates by `(tool_name, args_hash)` and
    satisfy the qualifying-pair rule from the module docstring.

    Guarantees: `member_node_ids` has at least 2 entries, sorted ascending; exactly one of
    them equals `representative_node_id` (the member whose own duration is *not* wasted -
    the largest-duration member, ties broken by `(start_virtual_ts_ms, node_id)` ascending,
    so the earliest-and-cheapest member yields to a later, costlier one only when the later
    one is strictly larger); `wasted_virtual_ms = Sum(durations) - max(duration)` exactly, per
    PRD §16.3's own formula (which subtracts the max, independent of which member is kept -
    the two coincide here by construction); `evidence_seq` is every member's `anchor_seq`,
    sorted ascending (I6).
    """

    group_key: str
    tool_name: str
    args_hash: str
    member_node_ids: tuple[str, ...]
    representative_node_id: str
    wasted_virtual_ms: int
    wasted_tokens: int
    evidence_seq: tuple[int, ...]


def _group_key(tool_name: str, args_hash: str) -> str:
    """Return the redundancy group key.

    See the module docstring for why this composes the already-computed `args_hash` rather
    than re-hashing raw arguments.
    """
    h = hashlib.blake2b(digest_size=32)
    h.update(tool_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(args_hash.encode("utf-8"))
    return "blake2b:" + h.hexdigest()


def _happens_before(a: Mapping[str, int], b: Mapping[str, int]) -> bool:
    """Return whether vector clock `a` happens-before `b` (PRD §14.2, verbatim).

    Duplicated from `timing._happens_before` rather than imported — that name is private to
    `timing.py` by convention, and this pure, four-line function is cheaper to re-derive than
    to couple two sibling analysis modules over a leading-underscore import. Any divergence
    would be caught immediately by both modules' own tests against the same golden fixtures.
    """
    at_most = all(a.get(s, 0) <= b.get(s, 0) for s in a)
    strictly_less = any(a.get(s, 0) < b.get(s, 0) for s in sorted(set(a) | set(b)))
    return at_most and strictly_less


def _concurrent(a: Mapping[str, int], b: Mapping[str, int]) -> bool:
    """Return whether `a` and `b` are concurrent — neither happens-before the other."""
    return not _happens_before(a, b) and not _happens_before(b, a)


def _connected_components(
    node_ids: Iterable[str], pairs: Iterable[tuple[str, str]]
) -> dict[str, str]:
    """Union-find: return `{node_id: root}` where every pair in `pairs` shares a root.

    `root` is deterministically the lexicographically smallest member of its component, so
    the mapping is itself reproducible and independent of processing order (NFR-14). Defined
    once at module scope (rather than nested inside a caller's loop) so it never risks
    capturing a loop variable by reference.
    """
    parent = {node_id: node_id for node_id in node_ids}

    def find(node_id: str) -> str:
        root = node_id
        while parent[root] != root:
            root = parent[root]
        while parent[node_id] != root:
            parent[node_id], node_id = root, parent[node_id]
        return root

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    return {node_id: find(node_id) for node_id in sorted(parent)}


def _retry_components(nodes: Mapping[str, TimingNode]) -> dict[str, str]:
    """Return `{node_id: component_id}` over `retry_of` chains.

    Union-find over every node in `nodes` (not just a redundancy candidate bucket — a retry
    chain can legitimately change a tool's arguments partway through, so linkage must be
    resolved DAG-wide).
    """
    pairs = [
        (node_id, nodes[node_id].retry_of or node_id)
        for node_id in sorted(nodes)
        if nodes[node_id].retry_of is not None and nodes[node_id].retry_of in nodes
    ]
    return _connected_components(nodes.keys(), pairs)


def _intervening_state_change(a: TimingNode, b: TimingNode, dag: TimingDAG) -> bool:
    """Return whether some other node sits on `a`'s clock slot strictly between the two.

    See the module docstring's "tool's inputs" section for why this DAG-level proxy (rather
    than a precise state-key check the schema cannot support without raw argument bodies) is
    the conservative, log-derivable stand-in for "an intervening state change".
    """
    lo, hi = sorted((a.anchor_seq, b.anchor_seq))
    slot = a.clock_slot
    for node in dag.nodes.values():
        if node.node_id in (a.node_id, b.node_id):
            continue
        if node.clock_slot != slot:
            continue
        if lo < node.anchor_seq < hi:
            return True
    return False


def detect_redundancy(dag: TimingDAG) -> tuple[RedundancyGroup, ...]:
    """Return every redundancy group in `dag`, sorted by `(group_key, member_node_ids[0])`.

    PRD §16.3: exact-hash `(tool_name, args_hash)` buckets, filtered to genuinely qualifying
    pairs (see module docstring), reported as connected components of size >= 2. A hash
    bucket with, say, four members where only two of them qualify with each other is reported
    as a single group of those two — not a group of four (§16.3's conditions are pairwise;
    reporting non-qualifying members as "redundant" would be an unmeasured claim, I9).
    """
    buckets: dict[tuple[str, str], list[TimingNode]] = {}
    for node in dag.nodes.values():
        if node.kind != _TOOL_CALL_KIND or node.tool_name is None or node.args_hash is None:
            continue
        buckets.setdefault((node.tool_name, node.args_hash), []).append(node)

    retry_component = _retry_components(dag.nodes)

    groups: list[RedundancyGroup] = []
    for tool_name, args_hash in sorted(buckets):
        members = sorted(buckets[(tool_name, args_hash)], key=lambda n: n.node_id)
        if len(members) < 2:
            continue

        qualifying_pairs: list[tuple[str, str]] = []
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if retry_component[a.node_id] == retry_component[b.node_id]:
                    continue  # condition 2: retry-linked, never redundancy
                same_slot = a.clock_slot is not None and a.clock_slot == b.clock_slot
                if same_slot:
                    qualifies = not _intervening_state_change(a, b, dag)
                else:
                    qualifies = _concurrent(a.vclock, b.vclock)
                if qualifies:
                    qualifying_pairs.append((a.node_id, b.node_id))

        component_of = _connected_components((n.node_id for n in members), qualifying_pairs)
        components: dict[str, list[TimingNode]] = {}
        for node in members:
            components.setdefault(component_of[node.node_id], []).append(node)

        multi = [cid for cid in sorted(components) if len(components[cid]) >= 2]
        for idx, cid in enumerate(multi):
            group_members = sorted(components[cid], key=lambda n: n.node_id)
            durations = [n.duration_ms for n in group_members]
            # Representative: max duration, ties broken by (start_virtual_ts_ms, node_id) -
            # see the class docstring for why this coincides with PRD §16.3's own
            # "Sum(durations) - max(duration)" wasted-time formula.
            representative = max(
                group_members, key=lambda n: (n.duration_ms, -n.start_virtual_ts_ms, n.node_id)
            )
            key = _group_key(tool_name, args_hash)
            if len(multi) > 1:
                # Two or more disjoint qualifying components share the same (tool_name,
                # args_hash) bucket — disambiguate with a stable, evidence-derived suffix
                # rather than silently colliding their group_key (NFR-14: still deterministic,
                # since `idx` is assigned in sorted component-id order).
                key = f"{key}:{idx}"
            groups.append(
                RedundancyGroup(
                    group_key=key,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    member_node_ids=tuple(n.node_id for n in group_members),
                    representative_node_id=representative.node_id,
                    wasted_virtual_ms=sum(durations) - max(durations),
                    wasted_tokens=0,  # v1: no token accounting on tool_call spans (I9)
                    evidence_seq=tuple(sorted(n.anchor_seq for n in group_members)),
                )
            )

    groups.sort(key=lambda g: (g.group_key, g.member_node_ids[0]))
    return tuple(groups)


def duplicate_node_ids(groups: tuple[RedundancyGroup, ...]) -> frozenset[str]:
    """Return the node ids that are redundant *duplicates*, not each group's representative.

    Used by `overhead.redundant_work` to classify critical-path node durations (PRD §16.2.2:
    "minus one representative member").
    """
    return frozenset(
        node_id
        for group in groups
        for node_id in group.member_node_ids
        if node_id != group.representative_node_id
    )


__all__ = [
    "RedundancyGroup",
    "detect_redundancy",
    "duplicate_node_ids",
]

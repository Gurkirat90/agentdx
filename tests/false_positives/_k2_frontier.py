"""ADR-002's k=2 schedule frontier enumerator - test-only, minimal, no product surface.

`CONTEXT.md` §5 row 12b / ADR-002: "a minimal, test-only schedule enumerator ... It enumerates
the k=2 frontier and asserts an empty finding set; it has no CLI, no API field, no UI, no
reduction reporting and no coverage panel." §11.9b: "must never grow a CLI flag, an output
format or a reduction report ... a test fixture, not an early FR-6." This module is exactly
that and nothing more: one pure function, imported by `test_k2_frontier.py` alone.

**What "k=2" means here.** PRD §15.1: `k` bounds "the maximum number of scheduling points at
which the scheduler deviates from its default choice." This build has no real scheduler to
generate schedules from at all (`analysis.explore`'s bounded exploration is P13, out of this
prompt's scope) - what a *scheduling decision* even is has no product-code definition yet to
call into. `k2_frontier` below is therefore a self-contained, honest stand-in scoped to exactly
what gate G2 needs: given `n` independent (mutually concurrent, non-causally-related) events'
*default* relative order, enumerate every relative order reachable by at most `k=2` pairwise
transpositions of that order - each transposition standing in for "one point where scheduling
picked something other than the default next item." This is a real, bounded, deterministic
enumeration (not the full `n!` permutation space for `n > 4`, and never open-ended), directly
analogous to the concept PRD §15.1 names, without depending on any not-yet-built scheduler
internals to define a "scheduling point" for real.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations

MAX_TRANSPOSITIONS: int = 2
"""PRD §15.1's default `k`. Not a CLI flag, not configurable (ADR-002/§11.9b) - a single,
named constant this test-only module hardcodes, matching the product default it stands in for.
"""


def k2_frontier(default_order: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """Return every ordering of `default_order` reachable by <= `MAX_TRANSPOSITIONS` swaps.

    Deterministic and duplicate-free: returned as a sorted tuple of tuples (never a `set`
    iterated directly - NFR-14), always including `tuple(default_order)` itself (0 deviations
    is a valid schedule - the default one). Each swap exchanges the positions of two elements;
    up to `MAX_TRANSPOSITIONS` swaps are applied, in every combination, generating a bounded
    (not exhaustive-permutation) frontier around the default order.
    """
    n = len(default_order)
    frontier: set[tuple[str, ...]] = {tuple(default_order)}
    frontier |= _apply_up_to_k_transpositions(tuple(default_order), n, MAX_TRANSPOSITIONS)
    return tuple(sorted(frontier))


def _apply_up_to_k_transpositions(base: tuple[str, ...], n: int, k: int) -> set[tuple[str, ...]]:
    """Return every ordering reachable from `base` by exactly 1 through `k` swaps, inclusive."""
    result: set[tuple[str, ...]] = set()
    frontier_layer = {base}
    for _ in range(k):
        next_layer: set[tuple[str, ...]] = set()
        for perm in frontier_layer:
            for i, j in combinations(range(n), 2):
                swapped = list(perm)
                swapped[i], swapped[j] = swapped[j], swapped[i]
                next_layer.add(tuple(swapped))
        result |= next_layer
        frontier_layer = next_layer
    return result


__all__ = ["MAX_TRANSPOSITIONS", "k2_frontier"]

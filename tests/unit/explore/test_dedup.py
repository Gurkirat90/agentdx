"""Unit tests for `explore/dedup.py` — hand-computed expected outputs (AGENTS.md §5)."""

from __future__ import annotations

from agentdx.explore.dedup import SeenSchedules


def test_mark_returns_true_first_time_false_on_repeat() -> None:
    seen = SeenSchedules()
    assert seen.mark({1: 0}) is True
    assert seen.mark({1: 0}) is False


def test_mark_distinguishes_content_not_identity() -> None:
    """Two distinct dict objects with identical content are the same schedule."""
    seen = SeenSchedules()
    assert seen.mark({1: 0, 2: 1}) is True
    assert seen.mark({2: 1, 1: 0}) is False  # same content, different insertion order


def test_already_seen_does_not_mutate_state() -> None:
    seen = SeenSchedules()
    assert seen.already_seen({1: 0}) is False
    assert seen.already_seen({1: 0}) is False  # calling it again still says "no"
    seen.mark({1: 0})
    assert seen.already_seen({1: 0}) is True


def test_len_counts_distinct_signatures_only() -> None:
    seen = SeenSchedules()
    seen.mark({})
    seen.mark({1: 0})
    seen.mark({1: 0})  # duplicate, does not grow the count
    seen.mark({2: 0})
    assert len(seen) == 3


def test_empty_default_schedule_is_a_valid_signature() -> None:
    seen = SeenSchedules()
    assert seen.mark({}) is True
    assert seen.already_seen({}) is True

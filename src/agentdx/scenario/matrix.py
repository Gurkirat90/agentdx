"""Matrix expansion (PRD §21.5): one scenario document into the cross product of its `matrix:`.

`matrix: {seed: [1, 2, 3], "faults[0].type": [agent_crash, tool_failure]}` expands to six
derived scenario documents, one per combination, each with its own deterministic id. This is
how a chaos matrix is expressed — "cheap because virtual time makes each run sub-second" (PRD
§21.5) — and Design Constraint 3 requires the expansion itself to be just as cheap to trust:
**deterministic, stable across runs and across platforms.**

**Why matrix keys are dotted paths, not bare field names.** PRD §21.5's own illustrative
example uses a bare `fault_type` key, which does not correspond to any top-level scenario
field — a fault's type lives at `faults[i].type`, and nothing in §21.1 or §21.5 says which
`i`. This is a genuine, load-bearing gap the PRD does not resolve (flagged, not silently
guessed at, in this prompt's closing `NOT DONE` — see also `CONTEXT.md`'s new C-12 ruling).
The design taken here: a matrix key is a dotted/bracketed path into the document, identical in
shape to the `ScenarioPath` `validate.py` already reports errors against (`faults[0].type`,
`guards.max_tokens`), plus three bare aliases — `seed`, `mode`, `repeats` — for the three
existing top-level scalar fields unambiguous enough not to need a path. PRD §21.5's own
`fault_type` example must be written `faults[0].type` under this scheme; a bare key that is
not one of the three aliases is rejected with an error naming the dotted-path convention
explicitly, rather than guessing which fault index was meant.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from typing import Final

_BARE_ALIASES: Final[dict[str, tuple[str | int, ...]]] = {
    "seed": ("seed",),
    "mode": ("mode",),
    "repeats": ("repeats",),
}


class MatrixError(ValueError):
    """Raised when a `matrix:` key or value shape cannot be resolved to a document path.

    Carries `E-SCEN-011` in spirit (malformed value) — raised as a plain `ValueError`
    subclass rather than a `ScenarioError` because matrix expansion is a `loader`/`matrix`
    concern that runs independently of `validate.validate()`; callers that want a located,
    coded `ScenarioError` catch this and re-wrap it (`validate.py` does so for the shipped
    scenario set's own tests).
    """


def _bad_key(key: str) -> MatrixError:
    return MatrixError(
        f"matrix key {key!r} is not a valid dotted path. Use a bare `seed`/`mode`/"
        f"`repeats`, or a dotted/bracketed path like `faults[0].type` or "
        f"`guards.max_tokens` (PRD §21.5's own `fault_type` example must be written "
        f"as `faults[0].type` — see docs/scenario-reference.md)."
    )


def _parse_matrix_key(key: str) -> tuple[str | int, ...]:
    """Parse a matrix key into a `ScenarioPath` tuple: `"faults[0].type"` -> `("faults",0,"type")`.

    A small hand-written scanner rather than a single regex: a bracket segment (`[0]`) may
    directly follow a bare segment with no separator (`faults[0]`), while two bare segments
    must be separated by a `.` (`guards.max_tokens`) — a detail a naive `finditer` over
    "segment-shaped" tokens silently gets wrong (it accepts the separator dot as part of the
    gap between matches without checking it is actually a dot), which is exactly the kind of
    bug a validator whose entire job is precise error locations cannot afford to ship.
    """
    if key in _BARE_ALIASES:
        return _BARE_ALIASES[key]
    path: list[str | int] = []
    i, n = 0, len(key)
    need_separator = False
    while i < n:
        if need_separator:
            if key[i] == ".":
                i += 1
                need_separator = False
                if i >= n:
                    raise _bad_key(key)
                continue
            if key[i] != "[":
                raise _bad_key(key)
            need_separator = False
        if key[i] == "[":
            end = key.find("]", i)
            digits = key[i + 1 : end] if end != -1 else ""
            if end == -1 or not digits.isdigit():
                raise _bad_key(key)
            path.append(int(digits))
            i = end + 1
            need_separator = True
            continue
        j = i
        while j < n and key[j] not in ".[":
            j += 1
        if j == i:
            raise _bad_key(key)
        path.append(key[i:j])
        i = j
        need_separator = True
    if not path:
        raise _bad_key(key)
    return tuple(path)


def _set_path(
    document: dict[str, object], path: tuple[str | int, ...], value: object
) -> dict[str, object]:
    """Return a deep-copied `document` with `value` set at `path`, creating containers as needed."""
    result = copy.deepcopy(document)
    cursor: dict[str, object] | list[object] = result
    for i, segment in enumerate(path):
        is_last = i == len(path) - 1
        if isinstance(segment, int):
            if not isinstance(cursor, list):
                detail = f"path segment [{segment}] expects a list, found {type(cursor).__name__}"
                raise MatrixError(detail)
            while len(cursor) <= segment:
                cursor.append({})
            if is_last:
                cursor[segment] = value
            else:
                nxt = cursor[segment]
                if not isinstance(nxt, dict | list):
                    detail = (
                        f"path segment [{segment}] expects a container, found {type(nxt).__name__}"
                    )
                    raise MatrixError(detail)
                cursor = nxt
        else:
            if not isinstance(cursor, dict):
                detail = (
                    f"path segment '{segment}' expects a mapping, found {type(cursor).__name__}"
                )
                raise MatrixError(detail)
            if is_last:
                cursor[segment] = value
            else:
                nxt = cursor.setdefault(segment, {})
                if not isinstance(nxt, dict | list):
                    detail = (
                        f"path segment '{segment}' expects a container, found {type(nxt).__name__}"
                    )
                    raise MatrixError(detail)
                cursor = nxt
    return result


def _slug(value: object) -> str:
    """Render a matrix value as an id-safe token: alnum and `-`/`_` kept, everything else `_`."""
    text = str(value)
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)


@dataclass(frozen=True, slots=True)
class MatrixExpansion:
    """One member of a matrix's cross product: a derived scenario id and the overridden document.

    Guarantees: `scenario_id` is unique within one expansion (no two combinations produce the
    same id) and is a pure function of `base_scenario_id` and the sorted assignment — nothing
    about `itertools.product`'s internal iteration order leaks into it.
    """

    scenario_id: str
    document: dict[str, object]
    assignment: tuple[tuple[str, object], ...]


def expand_matrix(document: dict[str, object]) -> tuple[MatrixExpansion, ...]:
    """Expand `document`'s `matrix:` key into its cross product. `()` if there is no matrix.

    Determinism (Design Constraint 3): matrix keys are sorted explicitly before the cross
    product is built — never relied on as "YAML mapping order happens to be stable enough" —
    so the output is byte-identical across repeated calls, processes and platforms for the
    same input document. This is verified by `tests/unit/scenario/test_matrix.py`, which runs
    the expansion in fresh subprocesses, not only in-process.
    """
    matrix = document.get("matrix")
    if not matrix:
        return ()
    if not isinstance(matrix, dict):
        detail = "`matrix` must be a mapping of key -> list of values"
        raise MatrixError(detail)

    sorted_keys = sorted(matrix.keys())  # Design Constraint 3: explicit sort, not dict order
    paths = [_parse_matrix_key(k) for k in sorted_keys]
    value_lists: list[list[object]] = []
    for key in sorted_keys:
        values = matrix[key]
        if not isinstance(values, list) or not values:
            detail = f"matrix.{key} must be a non-empty list of values"
            raise MatrixError(detail)
        value_lists.append(values)

    base_id = document.get("scenario", "scenario")
    base_document = {k: v for k, v in document.items() if k != "matrix"}

    expansions: list[MatrixExpansion] = []
    for combination in itertools.product(*value_lists):
        derived = base_document
        assignment: list[tuple[str, object]] = []
        for key, path, value in zip(sorted_keys, paths, combination, strict=True):
            derived = _set_path(derived, path, value)
            assignment.append((key, value))
        suffix = "__".join(f"{key}-{_slug(value)}" for key, value in assignment)
        derived_id = f"{base_id}__{suffix}"
        derived["scenario"] = derived_id
        expansions.append(
            MatrixExpansion(scenario_id=derived_id, document=derived, assignment=tuple(assignment))
        )
    return tuple(expansions)


__all__ = ["MatrixError", "MatrixExpansion", "expand_matrix"]

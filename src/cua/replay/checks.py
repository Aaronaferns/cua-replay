"""Evaluating predicates during a run: static ones over the observed screen, target ones against the live surface.

`target_visible` is true when the target resolves to exactly one element (it never raises for "not there").
`target_equals_input` reads the resolved element's text and compares it with the run's input value; if the target
cannot be resolved the resolution error propagates, because an identity check that cannot see its evidence has not
passed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from cua.artifact.names import normalize_name
from cua.artifact.predicates import (
    AllOf,
    AnyOf,
    Not,
    Predicate,
    ScreenView,
    TargetEqualsInput,
    TargetVisible,
    holds,
    is_static,
)
from cua.replay.resolver import ResolutionError

# resolve(target_id) -> element (raises ResolutionError); read(element) -> text
Resolve = Callable[[str], Awaitable[object]]
Read = Callable[[object], Awaitable[str]]


def text_equals_input(shown: str, wanted: str, normalizer: str) -> bool:
    if normalizer == "exact":
        return shown.strip() == wanted.strip() != ""
    key = normalize_name(wanted)
    return bool(key) and normalize_name(shown) == key


async def evaluate(pred: Predicate, *, view: ScreenView, inputs: dict[str, str], resolve: Resolve, read: Read) -> bool:
    if is_static(pred):
        return holds(pred, view)
    if isinstance(pred, TargetVisible):
        try:
            await resolve(pred.target)
        except ResolutionError:
            return False
        return True
    if isinstance(pred, TargetEqualsInput):
        element = await resolve(pred.target)
        return text_equals_input(await read(element), inputs[pred.input], pred.normalizer)
    if isinstance(pred, AllOf):
        for child in pred.of:
            if not await evaluate(child, view=view, inputs=inputs, resolve=resolve, read=read):
                return False
        return True
    if isinstance(pred, AnyOf):
        for child in pred.of:
            if await evaluate(child, view=view, inputs=inputs, resolve=resolve, read=read):
                return True
        return False
    if isinstance(pred, Not):
        return not await evaluate(pred.of, view=view, inputs=inputs, resolve=resolve, read=read)
    raise TypeError(f"unsupported predicate {pred.kind}")

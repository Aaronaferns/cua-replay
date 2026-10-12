"""Template-proposed states: a screen the profile does not know becomes a candidate `observed` state.

The template is deliberately small: the URL path as a glob, plus the first main-content heading when the page has one.
Nothing else (no title, no record names, no field values) can enter a signature, so a proposal is a statement about
the *kind* of page. A proposal is only a proposal until `collisions` is empty: adding it must not change how any
screen already seen is classified, and every screen it was proposed for must classify as it.

Leaf module: this package and the standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from cua.artifact.classify import classify
from cua.artifact.names import normalize_name
from cua.artifact.predicates import AllOf, Predicate, ScreenView, Text, UrlMatches, holds
from cua.artifact.schema import Profile, State


def url_glob(url: str) -> str:
    """`/comm/card.php?socid=63` -> `/comm/card.php*`: the path, whatever the query."""
    return f"{urlsplit(url).path or '/'}*"


def template_signature(url: str, heading: str | None) -> Predicate:
    """URL glob + first main-content heading. A page without a heading is recognised by its URL alone."""
    glob = UrlMatches(pattern=url_glob(url))
    if heading and normalize_name(heading):
        return AllOf(of=[glob, Text(equals=heading, role="heading")])
    return glob


def proposed_id(url: str, taken: Sequence[str]) -> str:
    """`/comm/card.php` -> `comm_card` (numbered if that id is taken)."""
    stem = re.sub(r"\.[a-z0-9]+$", "", urlsplit(url).path.lower())
    base = re.sub(r"[^a-z0-9]+", "_", stem).strip("_") or "page"
    if not base[0].isalpha():
        base = f"page_{base}"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    return sid


def propose_state(url: str, heading: str | None) -> State:
    anchor = f"the heading {heading!r}" if heading else "no heading (the URL path alone)"
    return State(
        description=f"Proposed from a recorded screen at {url_glob(url)}; recognised by {anchor}.",
        layer="page",
        signature=template_signature(url, heading),
        origin="observed",
    )


def with_states(profile: Profile, proposals: dict[str, State]) -> Profile:
    return profile.model_copy(update={"states": {**profile.states, **proposals}})


def collisions(profile: Profile, proposals: dict[str, State], seen: Sequence[tuple[ScreenView, str]]) -> list[str]:
    """Problems if `proposals` are added to `profile`. `seen` pairs every screen with the state it must classify as.

    A collision is any seen screen that no longer classifies as expected: the proposal also matches a screen that
    belongs to another state (ambiguity), or the proposal fails to match the screen it was made for."""
    extended = with_states(profile, proposals)
    problems: list[str] = []
    done: set[tuple[str, str]] = set()
    for view, expected in seen:
        got = classify(extended, view)
        if got.kind == "state" and got.state == expected:
            continue
        key = (view.url.split("?")[0], expected)
        if key in done:
            continue
        done.add(key)
        problems.append(f"proposed states collide: the screen at {url_glob(view.url)} expected {expected!r} "
                        f"but is {got.label}" + (f" ({', '.join(got.candidates)})" if got.candidates else ""))
    return problems


def survival_edits(templates: dict[str, State], runs: Sequence[tuple[ScreenView, str]]) -> list[str]:
    """Spike S5: how many template signatures need a human edit to cover another run of the same capability?

    `templates` maps an id to its template state (one per distinct screen of the recorded trace); `runs` pairs every
    screen of the other run with the template id it must classify as. The templates are judged on their own, as a
    catalog of their own: a template needs an edit if it does not hold on its screen in the other run, or if another
    template also matches that screen. Returns the ids that need an edit (each once)."""
    shell = {sid: st for sid, st in templates.items()}
    needs: list[str] = []
    for view, expected in runs:
        matching = [sid for sid, st in shell.items() if holds(st.signature, view)]
        if matching == [expected]:
            continue
        if expected not in needs:
            needs.append(expected)
        # a template that also matches somebody else's screen is as much in need of an edit as the one it shadows
        for sid in matching:
            if sid != expected and sid not in needs:
                needs.append(sid)
    return needs

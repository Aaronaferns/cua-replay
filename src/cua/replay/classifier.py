"""Observation -> screen view -> state. The pure classification itself lives in `cua.artifact.classify`.

Settling (the navigation fence, then the same verdict on 2 polls at least 150 ms apart) is the interpreter's
`_settle`, which calls the surface's `settle()`; this module only turns one observation into a state.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from cua.artifact.classify import Classification, classify
from cua.artifact.predicates import ScreenNode, ScreenView
from cua.artifact.schema import Profile
from cua.artifact.screen import parse_outline
from cua.surface.base import Observation


def views_of(obs: Observation) -> tuple[ScreenView, ScreenView | None]:
    """(page view, dialog view or None). A dialog is present when the surface reports one."""
    page = ScreenView(url=obs.facts.url, nodes=parse_outline(obs.snapshot))
    dialog: ScreenView | None = None
    if obs.dialog_snapshot is not None or obs.facts.dialog is not None:
        nodes = list(parse_outline(obs.dialog_snapshot or ""))
        if obs.facts.dialog:
            nodes.append(ScreenNode(role="dialog", name=obs.facts.dialog))
        dialog = ScreenView(url=obs.facts.url, nodes=tuple(nodes))
    return page, dialog


def classify_observation(profile: Profile, obs: Observation) -> Classification:
    page, dialog = views_of(obs)
    return classify(profile, page, dialog)


def mask_url(url: str) -> str:
    """Path and query keys only: query values (record ids, tokens, search text) never enter a result."""
    parts = urlsplit(url)
    keys = [k for k, _ in parse_qsl(parts.query, keep_blank_values=True)]
    return (parts.path or "/") + (("?" + "&".join(f"{k}=*" for k in keys)) if keys else "")

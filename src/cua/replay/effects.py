"""The effect class replay assigns a step, whatever the artifact declares.

Commit routes come from the profile. A commit word in a control's name may raise the class, never lower it. A click
on an unnamed submit control in a dirty form (something was typed or selected since the last navigation) is a commit.
The result is never lower than the declared effect; the capability's declared effect is then a ceiling that the
interpreter enforces.
"""

from __future__ import annotations

import re

from cua.artifact.routes import is_commit
from cua.artifact.schema import EFFECT_RANK, ClickAction, Effect, Profile, Step, Target

COMMIT_WORDS = re.compile(r"\b(validate|confirm|pay|payment|paiement|delete|remove|approve)\b", re.IGNORECASE)

LINK_COMMIT_WORDS = re.compile(r"\b(validate|confirm|delete|remove|approve)\b", re.IGNORECASE)  # "Enter payment" only opens the form


def derived_effect(profile: Profile, step: Step, *, url: str | None, target: Target | None, dirty: bool,
                   prompts: bool = False) -> Effect:
    """`url` is the absolute URL a navigate step opens; `target` the artifact target a click acts on. `prompts`: the
    step leads to a more specific state of its own screen (a "Do you confirm ...?" prompt). Such a click declared
    below commit is not raised by its name (a "Pay" button that only asks); the commit is the later confirming step."""
    found: Effect = "none"
    if url is not None and is_commit(profile, "GET", url):
        found = "commit"
    if isinstance(step.do, ClickAction) and target is not None:
        names = [s.name for s in target.strategies if s.name]
        words = LINK_COMMIT_WORDS if target.fingerprint.role == "link" else COMMIT_WORDS
        if any(words.search(n) for n in names) and not (prompts and step.effect != "commit"):
            found = "commit"
        if dirty and not names and target.fingerprint.input_type == "submit":
            found = "commit"
    return max(step.effect, found, key=EFFECT_RANK.__getitem__)

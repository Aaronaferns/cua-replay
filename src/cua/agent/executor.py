"""The executor: turns a model-chosen action into a guarded surface call.

What happens to every action, in this order:
1. *Checks on the model's text.* A raw input value in ANY string argument is rejected, as are control characters and
   undeclared `{{tokens}}`. The model only ever writes tokens; this is where the value is substituted.
2. *Resolve* the ref to exactly one element of the current observation (a stale or unknown ref is an error, not a guess).
3. *Record* the element's locator candidates while its ref is live (recorder).
4. *Perform* through the surface, which asks the guard before touching the UI. A denial leaves the UI untouched.

Messages returned to the model never contain input or extracted values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from cua.agent.actions import (
    Click,
    Done,
    Extract,
    Navigate,
    Press,
    RequestApproval,
    RequestHelp,
    Select,
    Type,
    AgentAction,
    string_arguments,
)
from cua.artifact.names import normalize_name
from cua.artifact.schema import Effect, Profile
from cua.compiler.recorder import Recorder, actionable_text, value_cell
from cua.compiler.trace import TargetRecord
from cua.control.lease import LeaseToken
from cua.safety.policy import ActionKind, ActionRequest, Guard, PolicyDecision, PolicyDenied
from cua.surface import snapshot as snap
from cua.surface.base import Element, NativeDialogOpen, Observation, Surface, SurfaceError

_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_NUMBERS = re.compile(r"\d[\d.,]*\d|\d")
# A numeric record id literal in a URL query (socid=63, id=7, facid=216): it names this record only.
_RECORD_ID = re.compile(r"[?&;]\w*id=\d+", re.IGNORECASE)
_ICONS = re.compile(r"[-]")  # icon-font glyphs: a button named only by one is unnamed
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
# Substitution targets: only these arguments carry input tokens into the application.
_SUBSTITUTED = {"text", "option", "url"}
# Buttons whose name raises a click to a commit (the regex can only raise a class, never lower one).
_COMMIT_WORDS = re.compile(r"\b(delete|validate|confirm|pay|save|create|submit|modify)\b", re.IGNORECASE)


# Asks the console to approve one commit: (action hash, lease generation) -> approval id, or None to deny.
Approver = Callable[[str, int], Awaitable[str | None]]


class Rejected(Exception):
    """The action was refused before any element was resolved. The message is safe to show the model."""


@dataclass
class Outcome:
    status: str  # "performed" | "blocked" | "error"
    message: str  # for the model
    effect: Effect = "none"
    policy: PolicyDecision = field(default_factory=lambda: PolicyDecision(allowed=False, rule="not_evaluated", reason="no guard decision"))
    tokens: list[str] = field(default_factory=list)
    target: TargetRecord | None = None
    extracted: tuple[str, str] | None = None  # (output name, text) - never logged
    done_evidence: list[TargetRecord] = field(default_factory=list)
    terminal: str | None = None  # "done" | "help" | "approval"


def effect_of(action: AgentAction, node: snap.Node | None, target_url: str | None, profile: Profile,
              dirty: bool = False) -> Effect:
    """Effect class, decided here and never by the model: typing and selecting change nothing until submitted; routes
    the profile lists as commits (path + query globs) raise a navigation or link click to `commit`; so does a button
    named like a commit; so does an unnamed submit button once a field was typed or selected. Everything else that touches the page is a `read`."""
    if action.kind in ("type", "select"):
        return "none"
    if target_url and _commit_route(profile, target_url):
        return "commit"
    if action.kind == "click" and node is not None and node.role == "button" and _COMMIT_WORDS.search(node.name or ""):
        return "commit"
    if action.kind == "click" and node is not None and node.role == "button" and dirty and not _ICONS.sub("", node.name or "").strip():
        return "commit"
    return "read"


def _commit_route(profile: Profile, url: str) -> bool:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    for route in profile.commit_routes:
        if not fnmatchcase(parts.path or "/", route.path):
            continue
        if all(any(fnmatchcase(v, pattern) for v in query.get(k, [])) for k, pattern in route.query.items()):
            return True
    return False


class Executor:
    def __init__(
        self,
        *,
        surface: Surface,
        guard: Guard,
        lease: Callable[[], LeaseToken],
        recorder: Recorder,
        profile: Profile,
        inputs: Mapping[str, str],
        outputs: Mapping[str, object],
        actor: str = "agent",
        approve: Approver | None = None,
    ) -> None:
        self.surface, self.guard, self.lease, self.recorder, self.profile = surface, guard, lease, recorder, profile
        self._inputs = dict(inputs)
        self._outputs = set(outputs)
        self._actor = actor
        self._approve = approve  # None: every commit is denied
        self._used_approvals: set[str] = set()
        self._dirty = False  # a field was typed or selected since the last navigation or commit
        self.extracted: dict[str, str] = {}
        self._effect: Effect = "none"  # what the action in flight was classified as (kept for denials and errors)
        self._target: TargetRecord | None = None

    # -- checks on the model's text ----------------------------------------------------------------------------------

    def check_text(self, action: AgentAction) -> list[str]:
        """Raise `Rejected` for control characters, raw input values and undeclared tokens; return the tokens used."""
        used: list[str] = []
        for field_name, text in string_arguments(action):
            if _CONTROL.search(text):
                raise Rejected(f"argument '{field_name}' contains a control character; use plain single-line text")
            for token in _TOKEN.findall(text):
                if token not in self._inputs:
                    raise Rejected(f"argument '{field_name}' uses the undeclared placeholder {{{{{token}}}}}")
                if token not in used:
                    used.append(token)
            if self._contains_value(text):
                raise Rejected(f"argument '{field_name}' contains an input value; write its {{{{name}}}} placeholder")
            leftover = _TOKEN.sub("", text)
            if "{{" in leftover or "}}" in leftover:
                raise Rejected(f"argument '{field_name}' has a malformed placeholder")
        return used

    def _contains_value(self, text: str) -> bool:
        haystacks = {text.casefold(), normalize_name(text), quote(text, safe="").casefold()}
        for value in self._inputs.values():
            if not value:
                continue
            forms = {value.casefold(), normalize_name(value), quote(value, safe="").casefold()}
            for form in forms:
                if not form:
                    continue
                if len(form) < 4:  # too short to be told from ordinary words by substring: whole-argument match only
                    if any(h == form for h in haystacks):
                        return True
                elif any(form in h for h in haystacks):
                    return True
        return False

    def substitute(self, text: str, *, url: bool = False) -> str:
        return _TOKEN.sub(lambda m: quote(self._inputs[m.group(1)], safe="") if url else self._inputs[m.group(1)], text)

    # -- requests --------------------------------------------------------------------------------------------------

    def _request(self, kind: ActionKind, obs: Observation, effect: Effect, target_url: str | None = None,
                 approval_id: str | None = None) -> ActionRequest:
        return ActionRequest(kind=kind, actor=self._actor, page_url=obs.facts.url, target_url=target_url,  # type: ignore[arg-type]
                             effect=effect, lease=self.lease(), approval_id=approval_id)

    async def _gate(self, action: AgentAction, effect: Effect, target: TargetRecord | None, url: str | None,
                    tokens: list[str]) -> tuple[str | None, Outcome | None]:
        """A commit needs a single-use approval bound to (action hash, lease generation). Returns (approval id, None)
        to go ahead, or (None, the terminal outcome) when it is denied; nothing has touched the UI then."""
        if effect != "commit":
            return None, None
        generation = self.lease().generation
        locators = sorted(s for c in (target.candidates if target else []) for s in c.strategy.locator_strings())
        digest = hashlib.sha256(json.dumps([action.kind, url, tokens, locators]).encode()).hexdigest()
        approval = await self._approve(digest, generation) if self._approve else None
        if approval and approval not in self._used_approvals:
            self._used_approvals.add(approval)
            return approval, None
        decision = PolicyDecision(allowed=False, rule="approval_required", reason="a commit needs a console approval",
                                  approvable=True)
        return None, Outcome("blocked", "Stopping: this action commits and was not approved. Nothing was done.",
                             effect, decision, tokens, target, terminal="approval")

    @staticmethod
    def _href(obs: Observation, ref: str) -> str | None:
        """The destination of a link, from the `/url:` line under its node, made absolute."""
        nodes = snap.parse(actionable_text(obs))
        for i, node in enumerate(nodes):
            if node.ref == ref:
                for child in nodes[i + 1 :]:
                    if child.depth <= node.depth:
                        break
                    if child.role == "/url" and child.value:
                        return urljoin(obs.facts.url, child.value)
                return None
        return None

    # -- execution -------------------------------------------------------------------------------------------------

    async def execute(self, action: AgentAction, obs: Observation) -> Outcome:
        try:
            tokens = self.check_text(action)
        except Rejected as e:
            return Outcome("error", f"Rejected: {e}.")
        self._effect, self._target = "none", None
        try:
            return await self._execute(action, obs, tokens)
        except NativeDialogOpen:
            return Outcome("error", "A native browser dialog is blocking the page. It cannot be answered by tools; "
                                    "call request_help.", self._effect, tokens=tokens, target=self._target)
        except PolicyDenied as e:
            return Outcome("blocked", f"Denied by policy ({e.decision.rule}): {e.decision.reason}. Nothing was done.",
                           self._effect, policy=e.decision, tokens=tokens, target=self._target)
        except SurfaceError as e:
            return Outcome("error", f"The action could not be carried out: {e}. Read the current page and retry.",
                           self._effect, tokens=tokens, target=self._target)
        except ValueError as e:  # a surface-level argument check (key not allowed, URL not absolute)
            return Outcome("error", f"Rejected: {e}.", self._effect, tokens=tokens, target=self._target)

    async def _resolve(self, ref: str, obs: Observation, *, read_only: bool = False
                       ) -> tuple[Element, snap.Node | None, str]:
        """The element for a ref, its snapshot node and the ref actually used. For read-only uses (extract, done
        evidence) text sitting in a nameless table cell is recorded as the cell: that is the element a label's row
        leads to, and what a later replay can find."""
        nodes = snap.parse(actionable_text(obs))
        node = next((n for n in nodes if n.ref == ref), None)
        if read_only and node is not None:
            lifted = value_cell(nodes, nodes.index(node))
            if lifted is not node and lifted.ref:
                node, ref = lifted, lifted.ref
        return await self.surface.element_for_ref(ref, obs.epoch), node, ref

    def _stable(self, target: TargetRecord) -> TargetRecord:
        """Drop candidates that mention an extracted value (an accessible name that contains the amount changes with
        the amount, so it would find nothing for the next customer)."""
        volatile = {m for text in self.extracted.values() for m in _NUMBERS.findall(text)}
        if not volatile:
            return target
        kept = [c for c in target.candidates
                if not any(v in s for s in c.strategy.locator_strings() for v in volatile)]
        return target.model_copy(update={"candidates": kept})

    async def _record(self, element: Element, node: snap.Node | None, ref: str, obs: Observation) -> TargetRecord:
        role = node.role if node else ""
        name = (node.name or node.value) if node else None
        return await self.recorder.target(element, ref=ref, role=role, name=name, dialog=obs.facts.dialog)

    async def _execute(self, action: AgentAction, obs: Observation, tokens: list[str]) -> Outcome:
        match action:
            case RequestHelp():
                return Outcome("performed", "Stopping: help was requested.", tokens=tokens, terminal="help")
            case RequestApproval():
                return Outcome("performed", "Stopping: approval-gated actions are not available in this run.",
                               tokens=tokens, terminal="approval")
            case Done():
                missing = sorted(self._outputs - set(self.extracted))
                if missing:
                    return Outcome("error", f"Not done: extract these outputs first: {', '.join(missing)}.", tokens=tokens)
                if not action.evidence_refs:
                    return Outcome("error", "Not done: cite at least one element in evidence_refs.", tokens=tokens)
                resolved = [await self._resolve(ref, obs, read_only=True) for ref in action.evidence_refs]
                evidence = [self._stable(await self._record(el, node, ref, obs)) for el, node, ref in resolved]
                if bad := [r for r, e in zip(action.evidence_refs, evidence) if not e.candidates]:
                    return Outcome("error", "Not done: no stable way to find the element(s) " + ", ".join(bad)
                                   + " again was recorded; cite elements whose labels do not contain a value.",
                                   tokens=tokens)
                decision = PolicyDecision(allowed=True, rule="not_an_action", reason="done is not a surface action")
                return Outcome("performed", "Done.", effect="none", policy=decision, tokens=tokens,
                               done_evidence=evidence, terminal="done")
            case Press():
                effect = self._effect = effect_of(action, None, None, self.profile)
                decision = await self.surface.press(action.key, self._request("press", obs, effect))
                return Outcome("performed", f"Pressed {action.key}.", effect, decision, tokens)
            case Navigate():
                if _RECORD_ID.search(action.url):
                    return Outcome("error", "Rejected: a record id copied from the page does not carry over to another "
                                            "input. Reach the record by clicking its link or by filtering with the "
                                            "input placeholder.", tokens=tokens)
                url = self.substitute(action.url, url=True)
                effect = self._effect = effect_of(action, None, url, self.profile)
                approval, denied = await self._gate(action, effect, None, url, tokens)
                if denied:
                    return denied
                decision = await self.surface.navigate(url, self._request("navigate", obs, effect, url, approval))
                self._dirty = False
                return Outcome("performed", "Navigated.", effect, decision, tokens)
            case Click() | Type() | Select() | Extract():
                element, node, ref = await self._resolve(action.ref, obs, read_only=action.kind == "extract")
                href = self._href(obs, action.ref) if action.kind == "click" else None
                effect = self._effect = effect_of(action, node, href, self.profile, self._dirty)
                target = self._target = await self._record(element, node, ref, obs)
                if isinstance(action, Click):
                    approval, denied = await self._gate(action, effect, target, href, tokens)
                    if denied:
                        return denied
                    decision = await self.surface.click(element, self._request("click", obs, effect, href, approval))
                    self._dirty = self._dirty and effect != "commit"
                    return Outcome("performed", "Clicked.", effect, decision, tokens, target)
                if isinstance(action, Type):
                    decision = await self.surface.type(element, self.substitute(action.text),
                                                       self._request("type", obs, effect))
                    self._dirty = True
                    return Outcome("performed", "Typed.", effect, decision, tokens, target)
                if isinstance(action, Select):
                    decision = await self.surface.select(element, self.substitute(action.option),
                                                         self._request("select", obs, effect))
                    self._dirty = True
                    return Outcome("performed", "Selected.", effect, decision, tokens, target)
                return await self._extract(action, element, obs, effect, tokens, target)
        raise AssertionError(f"unhandled action kind {action.kind}")  # pragma: no cover

    async def _extract(self, action: Extract, element: Element, obs: Observation, effect: Effect,
                       tokens: list[str], target: TargetRecord) -> Outcome:
        if action.name not in self._outputs:
            return Outcome("error", f"'{action.name}' is not a declared output.", tokens=tokens)
        if action.name in self.extracted:
            return Outcome("error", f"'{action.name}' was already extracted.", tokens=tokens)
        decision = self.guard.check(self._request("extract", obs, effect))
        if not decision.allowed:
            raise PolicyDenied(decision)
        text = await self.surface.read_text(element)
        if not text.strip():
            return Outcome("error", "The element shows no text to extract.", effect, decision, tokens, target)
        stable = self._stable_for(target, text)
        if not stable.candidates:
            return Outcome("error", "Nothing that survives a change of value could be recorded for this element (its "
                                    "label contains the value). Extract from an element whose label does not.",
                           effect, decision, tokens, target)
        self.extracted[action.name] = text
        return Outcome("performed", f"Extracted {action.name}.", effect, decision, tokens, stable,
                       extracted=(action.name, text))

    @staticmethod
    def _stable_for(target: TargetRecord, text: str) -> TargetRecord:
        volatile = set(_NUMBERS.findall(text))
        kept = [c for c in target.candidates
                if not any(v in s for s in c.strategy.locator_strings() for v in volatile)]
        return target.model_copy(update={"candidates": kept})

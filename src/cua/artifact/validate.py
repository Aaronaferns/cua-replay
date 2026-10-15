"""Static validation: does a capability make sense against its profile, before anything runs?

The schema rejects malformed documents field by field; this module checks the references *between* fields and
between the two documents. Both functions return human-readable problems (never values) and are empty when the
documents are consistent; `require_valid` raises `ArtifactInvalid` with all of them.

Leaf module: standard library plus this package.
"""

from __future__ import annotations

import re

from cua.artifact.predicates import Predicate, TargetEqualsInput, TargetVisible, Text, is_static, walk
from cua.artifact.schema import (
    EFFECT_RANK,
    TEMPLATE_RE,
    Action,
    AssertAction,
    Capability,
    ClickAction,
    ExtractAction,
    NavigateAction,
    Profile,
    SelectAction,
    Strategy,
    TypeAction,
    action_target,
    max_effect,
    template_refs,
)


class ArtifactInvalid(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def validate_profile(profile: Profile) -> list[str]:
    problems: list[str] = []
    states = profile.states
    if profile.entry.start_state not in states:
        problems.append(f"entry.start_state {profile.entry.start_state!r} is not a state")
    if profile.login.state not in states:
        problems.append(f"login.state {profile.login.state!r} is not a state")
    for sid, st in states.items():
        if not is_static(st.signature):
            problems.append(f"state {sid!r}: a signature may use static predicates only")
        if st.specializes is not None:
            if st.specializes not in states:
                problems.append(f"state {sid!r} specializes unknown state {st.specializes!r}")
            elif st.specializes == sid or sid in profile.ancestors(st.specializes):
                problems.append(f"state {sid!r}: specializes cycle")
            elif states[st.specializes].layer != st.layer:
                problems.append(f"state {sid!r} and its parent are on different layers")
    for sid in profile.default_meaning:
        if sid not in states:
            problems.append(f"default_meaning names unknown state {sid!r}")
    for hid, handler in profile.handlers.items():
        if handler.state not in states:
            problems.append(f"handler {hid!r} names unknown state {handler.state!r}")
        if handler.dismiss is not None:
            problems.extend(_strategy_problems(f"handler {hid!r}", handler.dismiss.strategies, set()))
    for label, target in (
        ("login.username", profile.login.username),
        ("login.password", profile.login.password),
        ("login.submit", profile.login.submit),
    ):
        problems.extend(_strategy_problems(label, target.strategies, set()))
    return problems


def _strategy_problems(where: str, strategies: list[Strategy], inputs: set[str]) -> list[str]:
    problems = []
    for s in strategies:
        for text in s.locator_strings():
            if "{{" in text:
                problems.append(f"{where}: an unlifted input token in a finder")
            for ref in template_refs(text):
                if ref not in inputs:
                    problems.append(f"{where}: finder references undeclared input {ref!r}")
    return problems


_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)\+?\d[\d ().-]{7,}\d(?!\d)")
_AMOUNT = re.compile(r"(?<!\d)\d[\d.,]*[.,]\d{2}(?!\d)")  # 199.75, 1,234.50, 12,50: a figure, not a label


def volatile_text(text: str) -> bool:
    """Text that is data rather than a label: an amount, an email address or a phone number. The compiler drops it
    from fingerprints and static validation refuses it in finders and fingerprints."""
    return bool(_AMOUNT.search(text) or _EMAIL.search(text) or _PHONE.search(text))


def _value_problems(where: str, texts: list[str], cap: Capability) -> list[str]:
    """No recorded value may survive into the artifact: a string that matches an input's pattern (a literal customer
    code left in a finder, a route or a predicate) or looks like an email address or phone number is a leak. The
    value itself is never repeated in the message."""
    problems: list[str] = []
    for text in texts:
        bare = TEMPLATE_RE.sub("", text)  # what is left once the lifted ${inputs} are removed
        for name, spec in cap.contract.inputs.items():
            if spec.pattern is not None and re.search(spec.pattern, bare):
                problems.append(f"{where}: contains a literal that matches input {name!r}; lift it to ${{{name}}}")
        if _EMAIL.search(bare) or _PHONE.search(bare):
            problems.append(f"{where}: contains what looks like personal data (an email address or phone number)")
    return problems


def _predicate_texts(pred: Predicate) -> list[str]:
    return [s for node in walk(pred) if isinstance(node, Text) for s in (node.equals, node.pattern) if s]


def _predicate_problems(where: str, pred: Predicate, targets: set[str], inputs: set[str]) -> list[str]:
    problems = []
    for node in walk(pred):
        if isinstance(node, TargetVisible | TargetEqualsInput) and node.target not in targets:
            problems.append(f"{where}: unknown target {node.target!r}")
        if isinstance(node, TargetEqualsInput) and node.input not in inputs:
            problems.append(f"{where}: unknown input {node.input!r}")
        if isinstance(node, Text):
            for s in (node.equals, node.pattern):
                if s and "{{" in s:
                    problems.append(f"{where}: an unlifted input token in a text predicate")
    return problems


def _action_templates(action: Action) -> list[str]:
    if isinstance(action, NavigateAction):
        return [action.route]
    if isinstance(action, TypeAction):
        return [action.text]
    if isinstance(action, SelectAction):
        return [action.option]
    return []


def validate_capability(cap: Capability, profile: Profile) -> list[str]:
    problems: list[str] = []
    states = profile.states
    inputs = set(cap.contract.inputs)
    outputs = set(cap.contract.outputs)
    targets = set(cap.targets)

    if cap.app != profile.app:
        problems.append(f"capability app {cap.app!r} does not match profile app {profile.app!r}")
    if cap.id.split(".", 1)[0] != cap.app:
        problems.append("capability id must start with '<app>.'")

    for name, target in cap.targets.items():
        problems.extend(_strategy_problems(f"target {name!r}", target.strategies, inputs))
        finders = [s for strategy in target.strategies for s in strategy.locator_strings()]
        problems.extend(_value_problems(f"target {name!r} finder", finders, cap))
        facts = [v for v in (target.fingerprint.row_label, target.fingerprint.column_header) if v]
        problems.extend(_value_problems(f"target {name!r} fingerprint", facts, cap))
        if any(_AMOUNT.search(f) for f in facts):
            problems.append(f"target {name!r} fingerprint: contains an amount; a fingerprint holds labels, not data")

    seen: set[str] = set()
    extracted: set[str] = set()
    for i, step in enumerate(cap.steps):
        where = f"step {step.id!r}"
        if step.id in seen:
            problems.append(f"{where}: duplicate step id")
        seen.add(step.id)
        if step.expect not in states:
            problems.append(f"{where}: expects unknown state {step.expect!r}")
        if EFFECT_RANK[step.effect] > EFFECT_RANK[cap.effect]:
            problems.append(f"{where}: effect {step.effect} exceeds the capability effect {cap.effect}")
        target_id = action_target(step.do)
        if target_id is not None and target_id not in targets:
            problems.append(f"{where}: unknown target {target_id!r}")
        if isinstance(step.do, ClickAction | TypeAction | SelectAction | ExtractAction) and target_id is None:
            problems.append(f"{where}: action needs a target")
        problems.extend(_value_problems(f"{where} action", _action_templates(step.do), cap))
        for pred in (step.verify, step.do.that if isinstance(step.do, AssertAction) else None):
            if pred is not None:
                problems.extend(_value_problems(f"{where} predicate", _predicate_texts(pred), cap))
        for text in _action_templates(step.do):
            if "{{" in text:
                problems.append(f"{where}: an unlifted input token in the action")
            for ref in template_refs(text):
                if ref not in inputs:
                    problems.append(f"{where}: action references undeclared input {ref!r}")
        if isinstance(step.do, ExtractAction):
            if step.do.output not in outputs:
                problems.append(f"{where}: extracts undeclared output {step.do.output!r}")
            extracted.add(step.do.output)
        if isinstance(step.do, AssertAction):
            problems.extend(_predicate_problems(where, step.do.that, targets, inputs))
        if step.verify is not None:
            problems.extend(_predicate_problems(where, step.verify, targets, inputs))
        for branch in step.on:
            if branch.state not in states:
                problems.append(f"{where}: branch on unknown state {branch.state!r}")
            if branch.outcome is not None and branch.outcome not in cap.contract.outcomes:
                problems.append(f"{where}: branch ends in undeclared outcome {branch.outcome!r}")
        if i == 0 and step.expect in states and profile.entry.start_state in states:
            if not profile.is_a(profile.entry.start_state, step.expect):
                problems.append(f"{where}: first step expects {step.expect!r} but the entry state is "
                                f"{profile.entry.start_state!r}")

    if cap.effect != max_effect([s.effect for s in cap.steps]):
        problems.append("capability effect must equal the maximum step effect")
    for step in cap.steps:
        if step.effect == "commit":
            if step.verify is None:
                problems.append(f"step {step.id!r}: a commit step needs a verify (the confirmation echo)")
            if not any(isinstance(o.do, AssertAction) and o.expect == step.expect for o in cap.steps):
                problems.append(f"step {step.id!r}: a commit step needs a review assert on its screen")

    if cap.success.state not in states:
        problems.append(f"success.state {cap.success.state!r} is not a state")
    for out in cap.success.outputs:
        if out not in outputs:
            problems.append(f"success lists undeclared output {out!r}")
        elif out not in extracted:
            problems.append(f"output {out!r} is not extracted by any step")
    if set(cap.success.outputs) != outputs:
        problems.append("success.outputs must list every declared output")
    for j, check in enumerate(cap.success.checks):
        problems.extend(_predicate_problems(f"success.checks[{j}]", check, targets, inputs))
        problems.extend(_value_problems(f"success.checks[{j}]", _predicate_texts(check), cap))
    for name, outcome in cap.contract.outcomes.items():
        for ret in outcome.returns:
            if ret not in outputs:
                problems.append(f"outcome {name!r} returns undeclared output {ret!r}")
    # every path that ends the run returns only what it has already produced: an outcome branch on step i may return
    # an output only if a step before i extracted it, and the success path (every step, in order) extracts them all
    produced: set[str] = set()
    for step in cap.steps:
        if isinstance(step.do, ExtractAction):
            produced.add(step.do.output)  # a branch is taken after its own step's action
        for branch in step.on:
            if branch.outcome in cap.contract.outcomes:
                missing = sorted(set(cap.contract.outcomes[branch.outcome].returns) - produced)
                if missing:
                    problems.append(f"step {step.id!r}: outcome {branch.outcome!r} returns {missing} "
                                    f"before any step has extracted them")
    return problems


def require_valid(cap: Capability, profile: Profile) -> None:
    problems = validate_profile(profile) + validate_capability(cap, profile)
    if problems:
        raise ArtifactInvalid(problems)

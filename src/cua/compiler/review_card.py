"""The review card: one deterministic Markdown page a second person reads before approving a capability.

It is rendered from the capability, the profile and the lock hash only (no run data, no clock, no input values), so
the same documents always give the same bytes and the card can be committed beside the capability as
`<capability>@<version>.review.md`. It shows what approval vouches for: the contract, every step with its intent and
effect, how each target is found (primary strategy and fallbacks), the success checkpoint, and the screen states the
capability relies on.

Imports: artifact only.
"""

from __future__ import annotations

from cua.artifact.lock import lock_hash
from cua.artifact.predicates import (
    AllOf,
    AnyOf,
    Not,
    Predicate,
    TargetEqualsInput,
    TargetVisible,
    Text,
    UrlMatches,
)
from cua.artifact.schema import (
    AssertAction,
    Capability,
    ClickAction,
    ExtractAction,
    FieldSpec,
    NavigateAction,
    PressAction,
    Profile,
    SelectAction,
    Step,
    Target,
    TypeAction,
)


def describe_predicate(p: Predicate) -> str:
    if isinstance(p, UrlMatches):
        return f"the URL matches `{p.pattern}`"
    if isinstance(p, Text):
        scope = "".join([f" ({p.role})" if p.role else "", f" in frame {p.frame}" if p.frame else ""])
        what = f'equals "{p.equals}"' if p.equals is not None else f"matches /{p.pattern}/"
        return f"some text{scope} {what}"
    if isinstance(p, TargetVisible):
        return f"target `{p.target}` is found exactly once"
    if isinstance(p, TargetEqualsInput):
        return f"target `{p.target}` equals input `{p.input}` ({p.normalizer} comparison)"
    if isinstance(p, AllOf):
        return "all of: " + "; ".join(describe_predicate(c) for c in p.of)
    if isinstance(p, AnyOf):
        return "any of: " + "; ".join(describe_predicate(c) for c in p.of)
    if isinstance(p, Not):
        return f"not ({describe_predicate(p.of)})"
    raise TypeError(type(p).__name__)  # pragma: no cover - the union is closed


def describe_action(step: Step) -> str:
    do = step.do
    if isinstance(do, NavigateAction):
        return f"navigate to `{do.route}`"
    if isinstance(do, ClickAction):
        return f"click `{do.target}`"
    if isinstance(do, TypeAction):
        return f"type `{do.text}` into `{do.target}`"
    if isinstance(do, SelectAction):
        return f"select `{do.option}` in `{do.target}`"
    if isinstance(do, PressAction):
        return f"press {do.key}"
    if isinstance(do, ExtractAction):
        return f"read `{do.target}` into output `{do.output}`"
    if isinstance(do, AssertAction):
        return f"assert that {describe_predicate(do.that)} (otherwise {do.on_fail})"
    raise TypeError(type(do).__name__)  # pragma: no cover


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _field_rows(fields: dict[str, FieldSpec]) -> list[str]:
    rows = ["| name | type | pattern | sensitivity | description |", "|---|---|---|---|---|"]
    for name, spec in fields.items():
        pattern = f"`{spec.pattern}`" if spec.pattern else ""
        rows.append(f"| `{name}` | {spec.type} | {_cell(pattern)} | {spec.sensitivity} | {_cell(spec.description)} |")
    return rows


def _states_used(cap: Capability, profile: Profile) -> list[str]:
    order: list[str] = [profile.entry.start_state]
    for step in cap.steps:
        order.append(step.expect)
        order.extend(b.state for b in step.on)
    order.extend([cap.success.state, profile.login.state])
    seen: list[str] = []
    for sid in order:
        for s in (sid, *profile.ancestors(sid)):
            if s in profile.states and s not in seen:
                seen.append(s)
    return seen


def _target_lines(name: str, target: Target) -> list[str]:
    fp = target.fingerprint
    facts = [f"role={fp.role}", f"tag={fp.tag}"]
    facts += [f"{k}={v}" for k, v in (("input_type", fp.input_type), ("frame", fp.frame),
                                      ("row_label", fp.row_label), ("column_header", fp.column_header)) if v]
    lines = [f"### `{name}`", "", target.description, "",
             f"- primary: {target.strategies[0].describe()}  ({target.strategies[0].why})"]
    for i, s in enumerate(target.strategies[1:], start=1):
        lines.append(f"- fallback {i}: {s.describe()}  ({s.why}); used only if it agrees with the fingerprint")
    lines.append(f"- fingerprint: {', '.join(facts)}")
    return lines + [""]


def render_review_card(cap: Capability, profile: Profile) -> str:
    h = lock_hash(cap, profile)
    out: list[str] = [
        f"# Review card: {cap.title}",
        "",
        f"`{cap.id}@{cap.version}` | effect **{cap.effect}** | app `{cap.app}`",
        "",
        f"- lock hash: `{h}`",
        f"- discovered by: {cap.provenance.discovered_by} (compiler `{cap.provenance.compiler}`, "
        f"trace `{cap.provenance.trace_run_id}`, sha256 `{cap.provenance.trace_sha256}`)",
        "- approval binds to the lock hash above: any change to the capability or the app profile makes it stale",
        "",
        cap.description,
        "",
        "## Contract",
        "",
        "### Inputs",
        "",
        *_field_rows(cap.contract.inputs),
        "",
        "### Outputs",
        "",
        *_field_rows(cap.contract.outputs),
        "",
        "### Business outcomes",
        "",
    ]
    if cap.contract.outcomes:
        out += [f"- `{name}`: {o.description}" + (f" (returns {', '.join(o.returns)})" if o.returns else "")
                for name, o in cap.contract.outcomes.items()]
    else:
        out.append("- none declared")
    out += ["", "## Steps", ""]
    for i, step in enumerate(cap.steps, start=1):
        flags = f"effect **{step.effect}**" + (", navigates" if step.navigates else "")
        out += [f"{i}. `{step.id}`: {step.intent}",
                f"   - starts in state `{step.expect}`; {describe_action(step)}; {flags}; timeout {step.timeout_ms} ms"]
        for b in step.on:
            dest = "continues" if b.then else f"ends with the business outcome `{b.outcome}`"
            out.append(f"   - on state `{b.state}`: {dest}")
        if step.verify is not None:
            out.append(f"   - verify: {describe_predicate(step.verify)}")
    commits = [s.id for s in cap.steps if s.effect == "commit"]
    out += ["", f"Commit steps: {', '.join(f'`{c}`' for c in commits) if commits else 'none'}", ""]
    if commits:
        out += ["Requests the profile treats as commits:", ""]
        for r in profile.commit_routes:
            parts = [r.method, r.path] + [f"query {k}={v}" for k, v in r.query.items()]
            parts += [f"body {k}={v}" for k, v in r.body.items()]
            out.append(f"- {' '.join(parts)}")
        out.append("")
    out += ["## Targets", ""]
    for name, target in cap.targets.items():
        out += _target_lines(name, target)
    out += ["## Success checkpoint", "", f"- ends in state `{cap.success.state}` (or a specialization of it)",
            f"- outputs: {', '.join(f'`{o}`' for o in cap.success.outputs)}"]
    out += [f"- check: {describe_predicate(c)}" for c in cap.success.checks]
    out += ["", "## States used", ""]
    for sid in _states_used(cap, profile):
        st = profile.states[sid]
        extra = [f"origin {st.origin}" + (", edited by a human" if st.edited else ""), f"layer {st.layer}"]
        if st.specializes:
            extra.append(f"specializes `{st.specializes}`")
        meaning = profile.default_meaning.get(sid)
        if meaning is not None:
            extra.append("default meaning: " + (f"outcome `{meaning.outcome}`" if meaning.outcome
                                                 else f"failure `{meaning.failure}`"))
        out += [f"- `{sid}`: {st.description} ({'; '.join(extra)})",
                f"  - recognised when {describe_predicate(st.signature)}"]
    out.append("")
    return "\n".join(out)

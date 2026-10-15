"""A commit in discovery needs a console approval bound to (action hash, lease generation), single use."""

from __future__ import annotations

import re

import pytest
from fakesite import LEASE
from scripts import code_filter_ref
from test_loop_offline import build

from cua.agent.actions import Click, Type
from cua.agent.scripted_adapter import ScriptedAdapter
from cua.surface import snapshot as snap

pytestmark = pytest.mark.asyncio(loop_scope="module")


def unnamed_submit(obs):
    return next(n.ref for n in snap.parse(obs.snapshot) if n.role == "button" and not re.sub("[-]", "", n.name or "").strip() and n.ref)


def steps():
    return [
        lambda obs: Type(rationale="Fill the filter", ref=code_filter_ref(obs), text="{{customer_code}}"),
        lambda obs: Click(rationale="Submit the form", ref=unnamed_submit(obs)),
    ]


def clicks(guard):
    return [c for c in guard.calls if c.kind == "click"]


async def test_commit_without_approver_is_denied_and_never_clicked(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(steps()))
    result = await loop.run()
    assert result.stop_reason == "approval_denied" and result.outcome == "stopped"
    last = result.trace.actions[-1]
    assert last.effect == "commit" and last.status == "blocked"
    assert not clicks(guard)
    writer.close()


async def test_denying_approver_stops_before_the_click(fake_surface, tmp_path):
    surface, page, guard = fake_surface
    asked = []

    async def deny(digest, generation):
        asked.append((digest, generation))

    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(steps()), approve=deny)
    result = await loop.run()
    assert result.stop_reason == "approval_denied" and not clicks(guard)
    assert len(asked) == 1 and asked[0][1] == LEASE.generation
    writer.close()


async def test_approved_commit_is_clicked_with_the_approval_id_and_traced(fake_surface, tmp_path):
    surface, page, guard = fake_surface

    async def approve(digest, generation):
        return "ap-1"

    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(steps()), approve=approve)
    result = await loop.run()
    assert result.stop_reason == "budget" or result.outcome == "stopped"
    click = result.trace.actions[1]
    assert click.effect == "commit" and click.status == "performed" and click.post is not None
    assert [c.approval_id for c in clicks(guard)] == ["ap-1"]
    writer.close()


async def test_approval_is_single_use(fake_surface, tmp_path):
    surface, page, guard = fake_surface

    async def reuse(digest, generation):
        return "ap-1"  # the same approval offered again

    again = steps() + steps()
    loop, writer = build(surface, guard, tmp_path, ScriptedAdapter(again), approve=reuse)
    result = await loop.run()
    assert result.stop_reason == "approval_denied"
    assert [c.approval_id for c in clicks(guard)] == ["ap-1"]
    writer.close()

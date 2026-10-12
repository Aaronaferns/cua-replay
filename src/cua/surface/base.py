"""The surface seam: how we perceive and act on an application, independent of the recorded flow.

Owner: Agent A (runtime + discovery). Seeded by the orchestrator with the shared types only; A owns their evolution,
other agents request changes through the orchestrator.

Discovery acts on ephemeral refs from an observation. Replay acts on elements found from artifact `Strategy`s.
Refs never enter an artifact. Every action method takes an `ActionRequest` and must pass it to the guard *before*
touching the UI; a denial raises `PolicyDenied` with the UI untouched. Only the web implementation exists; a desktop
surface would implement the same protocol (UIA role + name) and is design-only.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from cua.artifact.schema import Fingerprint, Strategy
from cua.safety.policy import ActionRequest, PolicyDecision


class SurfaceError(Exception):
    """A surface operation could not be carried out as asked. Messages never contain typed or extracted values."""


class StaleRefError(SurfaceError):
    """The ref belongs to an older observation, or its element is gone: observe again."""


class UnknownRefError(SurfaceError):
    """The ref was not shown in the current observation (outside the observed scope, or invented)."""


class NotUniqueError(SurfaceError):
    """A handle that must denote exactly one element matched none or several. Never resolved to 'the first'."""


class NativeDialogOpen(SurfaceError):
    """A native alert/confirm/prompt is blocking the page. It is never auto-accepted."""


class PageFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    dialog: str | None = None  # accessible name of an open in-page dialog, if any
    native_dialog: str | None = None  # message of a blocking alert/confirm, if any (never auto-accepted)
    frames_unavailable: bool = False  # a frame node came back empty (child-frame errors are swallowed)


class Observation(BaseModel):
    """What one observation of the surface saw. `snapshot` is scoped to the main content; refs are valid only for
    this `epoch`."""

    model_config = ConfigDict(extra="forbid")

    epoch: int
    facts: PageFacts
    snapshot: str  # AI snapshot of the main content area, with [ref=...] markers
    dialog_snapshot: str | None = None  # AI snapshot of the dialog layer, observed first
    menu_summary: list[str] = []  # navigation link names only
    screenshot_png: bytes | None = None  # in memory only; persisted masked, through the evidence writer


class Element(Protocol):
    """An opaque handle to exactly one element on the live surface."""


class Surface(Protocol):
    async def observe(self, *, screenshot: bool = True) -> Observation: ...

    # discovery: refs from the current epoch only; must resolve to exactly one element
    async def element_for_ref(self, ref: str, epoch: int) -> Element: ...

    # replay and recording: every element the strategy matches (the caller enforces exactly-one)
    async def find(self, strategy: Strategy) -> list[Element]: ...
    async def same_element(self, a: Element, b: Element) -> bool: ...
    async def fingerprint(self, element: Element) -> Fingerprint: ...
    async def read_text(self, element: Element) -> str: ...

    # actions: guard-checked inside, before the UI is touched. A denial raises PolicyDenied with the UI untouched;
    # an allowed action returns the guard's decision so the caller can record it. The guard is consulted exactly
    # once per action (single-use approvals depend on that).
    async def click(self, element: Element, request: ActionRequest) -> PolicyDecision: ...
    async def type(self, element: Element, text: str, request: ActionRequest) -> PolicyDecision: ...
    async def select(self, element: Element, option: str, request: ActionRequest) -> PolicyDecision: ...
    async def press(self, key: str, request: ActionRequest) -> PolicyDecision: ...
    async def navigate(self, url: str, request: ActionRequest) -> PolicyDecision: ...

    # evidence: a screenshot with the given elements (and, if generic, all inputs/cells) masked
    async def masked_screenshot(self, masks: list[Element], *, generic: bool) -> bytes: ...

    def describe(self) -> dict[str, Any]: ...  # surface kind and version facts for the run manifest

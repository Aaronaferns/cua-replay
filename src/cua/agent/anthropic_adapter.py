"""The Anthropic model adapter (`claude-opus-5-5`). The only adapter that imports the SDK.

Request rules this adapter follows (verified against the current API docs and the installed SDK):
- model id `claude-opus-5-5`, no date suffix; thinking is always on, so `thinking` is omitted (adaptive); no
  temperature or top_p; effort is set explicitly and never changed mid-run (it would invalidate the cache);
- all tools are declared on the first request and never change (strict, `additionalProperties: false`, every property
  required); `tool_choice` is `auto` with parallel tool use disabled (forced choice is a 400 for this model), and the
  system prompt says to call exactly one tool every turn; a turn without a tool call is a protocol problem;
- history is append-only: the full `response.content` (thinking blocks untouched) is the assistant turn, then one
  user turn with exactly one `tool_result` per tool call, then the new observation (image before text);
- top-level `cache_control` ephemeral; tools and system are byte-stable;
- `stop_reason == "refusal"` is checked before content is read; the beta server-side refusal fallback is opted into;
- usage and the request id are recorded per turn; errors are caught most-specific first.
"""

from __future__ import annotations

import base64
from typing import Any

import anthropic
from pydantic import ValidationError
from pydantic import SecretStr

from cua.agent.actions import ACTION_MODELS, parse_action
from cua.agent.model import Feedback, ModelTurn, Task, Usage, render_observation, tool_definitions
from cua.surface.base import Observation

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You operate a web application through a small set of tools to reach a goal. You see the page as text: an accessibility \
snapshot of the main content, with [ref=...] markers on elements you can act on. Menus are summarized as names only.

Rules:
- Call exactly one tool every turn, and nothing else. Every tool call needs a short single-line rationale.
- Act only on elements by the [ref=...] shown in the CURRENT page content. Refs from earlier turns are void.
- The goal and the tools use {{name}} placeholders for input values. Write the placeholder itself, never an actual \
value, in any argument. The system fills the value in when it acts.
- This is a read-only job: look things up, never change data. Do not click buttons that save, create, delete, \
validate, confirm or pay anything.
- Check that you are looking at the right record before you read from it: confirm that the identifying field on the \
page is the one you were asked for (an exact match, not a similar one).
- Your steps are recorded and replayed later for OTHER input values, so they must work for any input: reach a record \
by filtering with the placeholder and clicking its link, never by typing a record id (such as socid=63) that you \
read on the page into a URL. Navigating to a page by a fixed path and query is fine.
- Use extract(name, ref) to record each declared output from the element that shows it, then finish with \
done(evidence_refs) citing the elements on screen that prove the goal: the element that shows the identity you \
checked and the element you extracted from.
- If you cannot continue safely, call request_help.
- Arguments are plain single-line text.
"""


READ_ONLY_RULE = SYSTEM_PROMPT[SYSTEM_PROMPT.index("- This is a read-only job"):SYSTEM_PROMPT.index("- Check that you")]
WRITE_RULE = (
    "- This job makes exactly the one data change the goal asks for, once; make no other change. A person approves "
    "every data-changing click before it happens: just perform the click and the system asks them. If the application "
    "warns you or asks you to confirm something the goal did not ask for (such as an amount higher than what is owed), "
    "never answer yes: call request_help.\n"
)


def _first_message(task: Task, obs_text: str) -> str:
    inputs = "\n".join(f"- {{{{{n}}}}}: {s.description or s.type}" for n, s in task.inputs.items()) or "- (none)"
    outputs = "\n".join(f"- {n} ({s.type}): {s.description}".rstrip(": ") for n, s in task.outputs.items())
    return (
        f"Goal: {task.goal}\n\nInput placeholders:\n{inputs}\n\nOutputs to extract:\n{outputs}\n\n"
        f"The application is at {task.base_url} and you are already signed in. You may navigate by URL inside it or "
        f"use the links and forms on the page.\n\nCurrent screen:\n{obs_text}"
    )


def _image(png: bytes) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode("ascii")}}


class AnthropicAdapter:
    name = MODEL

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        effort: str = "high",
        screenshots: bool = False,
        writes: bool = False,
        client: Any | None = None,
    ) -> None:
        if client is None:
            if api_key is None or not api_key.get_secret_value():
                raise ValueError("ANTHROPIC_API_KEY is not configured")
            client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), timeout=180.0)
        self._client = client
        self.effort = effort
        self.screenshots = screenshots
        self._system = SYSTEM_PROMPT.replace(READ_ONLY_RULE, WRITE_RULE) if writes else SYSTEM_PROMPT
        self._tools = tool_definitions()
        self._messages: list[dict[str, Any]] = []
        self._pending: list[str] = []  # tool_use ids of the last assistant turn, still owed a tool_result
        self._task: Task | None = None

    async def start(self, task: Task) -> None:
        self._task = task
        self._messages = []
        self._pending = []

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    # -- one turn ------------------------------------------------------------------------------------------------

    def _observation_blocks(self, obs: Observation) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if self.screenshots and obs.screenshot_png:
            blocks.append(_image(obs.screenshot_png))
        blocks.append({"type": "text", "text": render_observation(obs)})
        return blocks

    def _next_user_turn(self, obs: Observation, feedback: Feedback | None) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if self._pending:
            first, *rest = self._pending
            ok = feedback.ok if feedback else True
            text = feedback.message if feedback else "ok"
            content.append({"type": "tool_result", "tool_use_id": first, "content": text, "is_error": not ok})
            for extra in rest:
                content.append({"type": "tool_result", "tool_use_id": extra, "is_error": True,
                                "content": "Not executed: call exactly one tool per turn."})
        elif feedback is not None:
            content.append({"type": "text", "text": feedback.message})
        content += self._observation_blocks(obs)
        return {"role": "user", "content": content}

    async def next_action(self, obs: Observation, feedback: Feedback | None = None) -> ModelTurn:
        if self._task is None:
            raise RuntimeError("start() the adapter before asking for an action")
        if not self._messages:
            blocks = self._observation_blocks(obs)
            blocks[-1] = {"type": "text", "text": _first_message(self._task, render_observation(obs))}
            self._messages.append({"role": "user", "content": blocks})
        else:
            self._messages.append(self._next_user_turn(obs, feedback))
        self._pending = []

        try:
            message = await self._client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self._system,
                tools=self._tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                messages=self._messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=BETAS,
                fallbacks="default",
            )
        # most specific first; the SDK has already retried 429 and 5xx
        except anthropic.BadRequestError as e:
            return self._error("bad_request", e)
        except anthropic.AuthenticationError as e:
            return self._error("authentication", e)
        except anthropic.PermissionDeniedError as e:
            return self._error("permission_denied", e)
        except anthropic.NotFoundError as e:
            return self._error("not_found", e)
        except anthropic.RateLimitError as e:
            return self._error("rate_limited", e)
        except anthropic.APIStatusError as e:
            return self._error("api_status", e)
        except anthropic.APIConnectionError as e:
            return self._error("connection", e)

        usage = self._usage(message)
        if message.stop_reason == "refusal":  # before any content is read
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            return ModelTurn(stop="refusal", detail=f"refusal{f' ({category})' if category else ''}", usage=usage)

        # append-only history: the full content, untouched (thinking blocks included)
        content = [block.model_dump(mode="json", exclude_none=True) for block in message.content]
        self._messages.append({"role": "assistant", "content": content})
        calls = [b for b in content if b.get("type") == "tool_use"]
        self._pending = [c["id"] for c in calls]

        if message.stop_reason == "max_tokens":
            return ModelTurn(stop="no_action", detail="the reply hit max_tokens before a tool call", usage=usage)
        if not calls:
            return ModelTurn(stop="no_action", detail="the reply contained no tool call", usage=usage)
        call = calls[0]
        if call["name"] not in ACTION_MODELS:
            return ModelTurn(stop="invalid_action", detail=f"unknown tool {call['name'][:40]!r}", usage=usage)
        try:
            action = parse_action(call["name"], dict(call["input"]))
        except ValidationError as e:
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()})
            return ModelTurn(stop="invalid_action", detail=f"invalid arguments for {call['name']}: {fields}",
                             usage=usage)
        return ModelTurn(action=action, usage=usage)

    # -- helpers -------------------------------------------------------------------------------------------------

    @staticmethod
    def _usage(message: Any) -> Usage:
        u = message.usage
        return Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            request_id=getattr(message, "_request_id", None),
        )

    @staticmethod
    def _error(category: str, e: anthropic.APIError) -> ModelTurn:
        status = getattr(e, "status_code", None)
        detail = f"{category}" + (f" (HTTP {status})" if status else "")
        if isinstance(e, anthropic.BadRequestError):  # a schema or history problem: the text is how it is fixed
            detail += f": {str(getattr(e, 'message', ''))[:300]}"
        return ModelTurn(stop="error", detail=detail, usage=Usage(request_id=getattr(e, "request_id", None)))

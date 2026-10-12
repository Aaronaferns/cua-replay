"""The Anthropic adapter against a fake client: request shape, append-only history, protocol problems, refusals."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from cua.agent.anthropic_adapter import BETAS, MODEL, SYSTEM_PROMPT, AnthropicAdapter
from cua.agent.model import Feedback, Task, tool_definitions
from cua.artifact.schema import FieldSpec
from cua.surface.base import Observation, PageFacts

TASK = Task(goal="Find {{customer_code}}", inputs={"customer_code": FieldSpec(type="string")},
            outputs={"amount": FieldSpec(type="decimal")}, base_url="http://localhost:8085")


class Block:
    def __init__(self, **data):
        self.data = data

    def model_dump(self, **_):
        return copy.deepcopy(self.data)


def message(*blocks, stop="tool_use", details=None):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=5, cache_read_input_tokens=7)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, stop_details=details, usage=usage,
                           _request_id="req_123")


def tool_use(id_, name, **input_):
    return Block(type="tool_use", id=id_, name=name, input=input_)


THINKING = Block(type="thinking", thinking="hmm", signature="sig")


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy({k: v for k, v in kwargs.items()}))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def obs(url="http://localhost:8085/x", png=None):
    return Observation(epoch=1, facts=PageFacts(url=url, title="T"), snapshot="- cell \"A\" [ref=e1]",
                       screenshot_png=png)


async def started(client, **kw):
    adapter = AnthropicAdapter(None, client=client, **kw)
    await adapter.start(TASK)
    return adapter


async def test_request_follows_the_api_rules():
    client = FakeClient([message(THINKING, tool_use("tu_1", "click", rationale="r", ref="e1"))])
    adapter = await started(client, effort="high")
    turn = await adapter.next_action(obs())
    req = client.requests[0]

    assert req["model"] == MODEL == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "high"}
    assert req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["betas"] == BETAS and req["fallbacks"] == "default"
    assert req["max_tokens"] == 16000 and req["system"] == SYSTEM_PROMPT
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "stream"):
        assert forbidden not in req
    assert [t["name"] for t in req["tools"]] == [
        "click", "type", "select", "press", "navigate", "extract", "request_approval", "request_help", "done"]
    for tool in req["tools"]:
        schema = tool["input_schema"]
        assert tool["strict"] is True and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"]) and "rationale" in schema["properties"]
    assert turn.action.kind == "click" and turn.action.ref == "e1"
    assert turn.usage.input_tokens == 100 and turn.usage.cache_read_input_tokens == 7
    assert turn.usage.request_id == "req_123"


async def test_goal_tokens_reach_the_model_but_no_values():
    client = FakeClient([message(tool_use("tu_1", "click", rationale="r", ref="e1"))])
    adapter = await started(client)
    await adapter.next_action(obs())
    first = client.requests[0]["messages"][0]["content"][-1]["text"]
    assert "{{customer_code}}" in first and "Outputs to extract" in first
    assert "api" not in first.lower().replace("application", "")  # no credentials or keys


def test_tools_and_system_are_byte_stable():
    assert tool_definitions() == tool_definitions()
    assert SYSTEM_PROMPT == SYSTEM_PROMPT.strip() + "\n" and "202" not in SYSTEM_PROMPT


async def test_history_is_append_only_with_full_content_and_one_tool_result_each():
    client = FakeClient([
        message(THINKING, tool_use("tu_1", "click", rationale="r", ref="e1")),
        message(THINKING, tool_use("tu_2", "press", rationale="r", key="Enter")),
    ])
    adapter = await started(client)
    await adapter.next_action(obs(png=b"\x89PNG"))
    await adapter.next_action(obs(), Feedback(ok=False, message="Denied by policy"))
    first, second = client.requests
    # the second request extends the first, untouched
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assistant = second["messages"][1]
    assert assistant["role"] == "assistant"
    assert [b["type"] for b in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
    user = second["messages"][2]
    results = [b for b in user["content"] if b["type"] == "tool_result"]
    assert user["content"][0]["type"] == "tool_result" and len(results) == 1
    assert results[0]["tool_use_id"] == "tu_1" and results[0]["is_error"] is True
    assert user["content"][-1]["type"] == "text"


async def test_screenshot_goes_before_the_text_only_when_enabled():
    client = FakeClient([message(tool_use("tu_1", "click", rationale="r", ref="e1")),
                         message(tool_use("tu_2", "click", rationale="r", ref="e1"))])
    with_shot = await started(client, screenshots=True)
    await with_shot.next_action(obs(png=b"\x89PNG\r\n"))
    content = client.requests[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"
    without = await started(client, screenshots=False)
    await without.next_action(obs(png=b"\x89PNG\r\n"))
    assert [b["type"] for b in client.requests[1]["messages"][0]["content"]] == ["text"]


async def test_a_turn_without_a_tool_call_is_a_protocol_problem_and_the_history_stays_valid():
    client = FakeClient([message(Block(type="text", text="I will look")),
                         message(tool_use("tu_2", "click", rationale="r", ref="e1"))])
    adapter = await started(client)
    turn = await adapter.next_action(obs())
    assert turn.action is None and turn.stop == "no_action"
    again = await adapter.next_action(obs(), Feedback(ok=False, message="Protocol error: call one tool"))
    assert again.action is not None
    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert not [b for b in second[2]["content"] if b["type"] == "tool_result"]
    assert second[2]["content"][0] == {"type": "text", "text": "Protocol error: call one tool"}


async def test_extra_parallel_tool_calls_each_get_a_result():
    client = FakeClient([message(tool_use("a", "click", rationale="r", ref="e1"),
                                 tool_use("b", "click", rationale="r", ref="e2")),
                         message(tool_use("c", "click", rationale="r", ref="e1"))])
    adapter = await started(client)
    await adapter.next_action(obs())
    await adapter.next_action(obs(), Feedback(ok=True, message="Clicked."))
    results = [b for b in client.requests[1]["messages"][2]["content"] if b["type"] == "tool_result"]
    assert [(r["tool_use_id"], r["is_error"]) for r in results] == [("a", False), ("b", True)]


async def test_invalid_arguments_and_unknown_tools_are_reported_not_raised():
    client = FakeClient([message(tool_use("a", "click", rationale="r")),  # missing ref
                         message(tool_use("b", "delete_everything", rationale="r"))])
    adapter = await started(client)
    assert (await adapter.next_action(obs())).stop == "invalid_action"
    assert (await adapter.next_action(obs(), Feedback(ok=False, message="x"))).stop == "invalid_action"


async def test_refusal_is_checked_before_content_and_stops_cleanly():
    details = SimpleNamespace(category="cyber")
    client = FakeClient([message(stop="refusal", details=details)])
    adapter = await started(client)
    turn = await adapter.next_action(obs())
    assert turn.stop == "refusal" and "cyber" in turn.detail and turn.action is None


def _status_error(cls, code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx.Response(code, request=request), body=None)


@pytest.mark.parametrize("exc,label", [
    (_status_error(anthropic.BadRequestError, 400), "bad_request"),
    (_status_error(anthropic.AuthenticationError, 401), "authentication"),
    (_status_error(anthropic.PermissionDeniedError, 403), "permission_denied"),
    (_status_error(anthropic.NotFoundError, 404), "not_found"),
    (_status_error(anthropic.RateLimitError, 429), "rate_limited"),
    (_status_error(anthropic.InternalServerError, 500), "api_status"),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "connection"),
])
async def test_errors_become_a_clean_stop(exc, label):
    adapter = await started(FakeClient([exc]))
    turn = await adapter.next_action(obs())
    assert turn.stop == "error" and turn.detail.startswith(label) and turn.action is None


def test_the_key_is_required_without_a_client():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        AnthropicAdapter(None)

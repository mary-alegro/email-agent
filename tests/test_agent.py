"""The PydanticAI extraction agent.

FunctionModel scripts the model's replies (no network), so these tests check the agent's
own behavior: the self-correction loop, its limits, and error mapping. The last two tests
send real requests through PydanticAI's Anthropic/OpenAI models to a mocked HTTP layer
and check what goes over the wire.
"""

import json
from datetime import date

import anthropic
import httpx2
import openai
import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from scripted import answer, retry_feedback, scripted

from src.agent.agent import READ_ONLY_ERP_TOOLS, ExtractionError, extract, get_agent
from src.agent.schemas import ERPContext
from src.agent.thread import parse_email
from src.config import settings

GOOD_QUOTE = "For PN JT-8831 we can ship 50 units in 6–8 weeks ARO."


def commitment(quote: str) -> dict:
    return {
        "po_number": "4500123456",
        "part_number": "JT-8831",
        "quantity": 50,
        "kind": "duration",
        "duration_min": 6,
        "duration_max": 8,
        "duration_unit": "weeks",
        "anchor": "ARO",
        "date": None,
        "is_option": False,
        "conditions": [],
        "assumptions": [],
        "evidence": [quote],
        "segment_index": 0,
        "confidence": 0.9,
    }


@pytest.fixture
def email(email_texts):
    return parse_email(email_texts["01"])


async def run(email):
    return await extract(email, ERPContext(), date(2026, 5, 20))


async def test_bad_quote_is_sent_back_and_corrected(email):
    feedback_seen = []

    def model(messages, info):
        feedback_seen.append(bool(retry_feedback(messages)))
        quote = GOOD_QUOTE if feedback_seen[-1] else "We ship in 2 weeks."  # invented at first
        return answer(commitment(quote))

    with scripted(model):
        result, usage = await run(email)
    assert feedback_seen == [False, True]  # one correction round
    assert result.commitments[0].evidence == [GOOD_QUOTE]
    assert usage.requests == 2


async def test_retries_are_bounded_then_the_code_gate_decides(email):
    calls = []

    def stubborn(messages, info):
        calls.append(1)
        return answer(commitment("We ship in 2 weeks."))

    with scripted(stubborn):
        result, _ = await run(email)
    assert len(calls) == 3  # first answer + 2 retries
    assert result.commitments[0].evidence == ["We ship in 2 weeks."]  # postprocess blocks it


async def test_unusable_output_becomes_extraction_error(email):
    with scripted(ModelResponse(parts=[TextPart("not json")])), pytest.raises(ExtractionError):
        await run(email)


def tool_names(body: dict) -> set[str]:
    return {t["name"] for t in body.get("tools", [])}


async def test_anthropic_request(email):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps({"commitments": []})}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    model = AnthropicModel("claude-opus-5-5", provider=AnthropicProvider(anthropic_client=client))
    with get_agent().override(model=model):  # the agent's own Anthropic settings stay
        await run(email)

    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["type"] == "json_schema"  # native structured output
    assert body["output_config"]["effort"] == "medium"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["beta"]
    assert "tool_choice" not in body or body["tool_choice"].get("type") == "auto"  # never forced
    assert tool_names(body) == READ_ONLY_ERP_TOOLS  # no write tool for the model


@pytest.mark.parametrize(
    "model_name, expected_effort",
    [
        ("gpt-5", "low"),  # PydanticAI knows this model supports reasoning effort
        ("unknown-model", None),  # unknown capabilities: the effort isn't sent (no 400)
    ],
)
async def test_openai_request(email, monkeypatch, model_name, expected_effort):
    # Configure the app for OpenAI and rebuild the agent from that configuration.
    # Only the model's capability profile matters here; nothing reaches OpenAI.
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", model_name)
    monkeypatch.setattr(settings, "llm_effort", "low")
    get_agent.cache_clear()
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        message = {"role": "assistant", "content": json.dumps({"commitments": []})}
        return httpx2.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": model_name,
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    client = openai.AsyncOpenAI(
        api_key="test",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        model = OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))
        with get_agent().override(model=model):
            await run(email)
    finally:
        get_agent.cache_clear()  # the next test rebuilds the default (Anthropic) agent

    body = captured["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body.get("reasoning_effort") == expected_effort
    assert body["messages"][0]["role"] in ("system", "developer")  # the instructions
    assert {t["function"]["name"] for t in body.get("tools", [])} == READ_ONLY_ERP_TOOLS

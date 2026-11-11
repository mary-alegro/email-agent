"""Helpers for scripting the real agent's model in tests (no LLM calls).

`scripted(...)` overrides the model of the real PydanticAI agent; everything else (the
output validator, the read-only MCP tools, the pipeline and the code gate) runs for real.

    with scripted(answer(commitment, ...)):         # always reply with this answer
        client.post("/emails", ...)

    with scripted(lambda messages, info: ...):      # full control (tool calls, retries)
        ...
"""

import json
from collections.abc import Callable
from contextlib import contextmanager

from pydantic import BaseModel
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile

from src.agent.agent import get_agent

ModelFn = Callable[[list, AgentInfo], ModelResponse]


def answer(*commitments: BaseModel | dict) -> ModelResponse:
    """A model reply with these commitments (ExtractedCommitment objects or dicts)."""
    items = [c.model_dump(mode="json") if isinstance(c, BaseModel) else c for c in commitments]
    return ModelResponse(parts=[TextPart(json.dumps({"commitments": items}))])


def scripted_model(reply: ModelResponse | ModelFn) -> FunctionModel:
    fn = reply if callable(reply) else (lambda messages, info: reply)
    return FunctionModel(fn, profile=ModelProfile(supports_json_schema_output=True))


@contextmanager
def scripted(reply: ModelResponse | ModelFn):
    with get_agent().override(model=scripted_model(reply)):
        yield


def retry_feedback(messages) -> list[str]:
    """The validator's correction requests the model received so far."""
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and "retry_feedback" in str(part.content)
    ]

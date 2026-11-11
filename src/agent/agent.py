"""The extraction agent (PydanticAI): reads one supplier email, proposes commitments.

- Model-agnostic: LLM_PROVIDER / LLM_MODEL pick Anthropic or OpenAI; output comes
  through the provider's native structured outputs (`NativeOutput`).
- Tools: the ERP's MCP server, filtered to read-only tools. The agent can look things up
  (a PO the regex missed, a part's lines, the lead time on file) but it can't write.
  Writing is done by the pipeline, after deterministic validation (postprocess.py).
- Output validator: sends hallucinations back to the model (`ModelRetry`): quotes, PO or
  part numbers that aren't in the email. These are pure text checks. Real conflicts in
  the email are not sent back; the code gate reports them. After the retry budget, the
  answer is accepted as is and the gate decides.

Tests swap the model with `get_agent().override(model=FunctionModel(...))`.
"""

import logging
import time
from datetime import date
from functools import cache

import pydantic_ai
from pydantic_ai import (
    AbstractToolset,
    Agent,
    AgentRunError,
    ModelRetry,
    NativeOutput,
    RunContext,
    UsageLimits,
)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from src.agent.mcp_client import erp_mcp_target
from src.agent.prompts import SYSTEM_PROMPT, build_user_message
from src.agent.schemas import AgentUsage, ERPContext, ExtractionResult
from src.agent.thread import ParsedEmail
from src.config import settings
from src.utils.text import mentioned, normalize_for_match

log = logging.getLogger(__name__)

# PydanticAI prints a promotional banner on first use; keep service logs clean.
pydantic_ai.BANNER_ENABLED = False

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
MAX_OUTPUT_TOKENS = 16000
# Bounds one email's run: model round trips (incl. output retries), ERP tool calls, tokens.
USAGE_LIMITS = UsageLimits(request_limit=8, tool_calls_limit=10, total_tokens_limit=200_000)
# The only ERP tools the agent gets. Writing (write_lead_times) is deliberately absent.
READ_ONLY_ERP_TOOLS = frozenset({"get_purchase_order", "find_po_lines", "get_lead_time"})


class ExtractionError(Exception):
    """The agent couldn't produce a result (API error, refusal, limits, bad output)."""


async def extract(
    email: ParsedEmail, context: ERPContext, reference_date: date
) -> tuple[ExtractionResult, AgentUsage]:
    """Run the agent on one email. Every agent failure becomes `ExtractionError`."""
    started = time.perf_counter()
    try:
        result = await get_agent().run(
            build_user_message(email, context, reference_date),
            deps=email,
            usage_limits=USAGE_LIMITS,
        )
    except AgentRunError as exc:  # API errors, refusals, usage limits, invalid output
        log.warning("Agent run failed: %s", exc, extra={"error": type(exc).__name__})
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc

    last = next((m for m in reversed(result.all_messages()) if isinstance(m, ModelResponse)), None)
    usage = AgentUsage(
        model=last.model_name if last else None,
        duration_ms=round((time.perf_counter() - started) * 1000),
        requests=result.usage.requests,
        tool_calls=result.usage.tool_calls,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
    )
    provider_request_id = last.provider_response_id if last else None
    log.info(
        "Agent run finished",
        extra={**usage.model_dump(), "provider_request_id": provider_request_id},
    )
    return result.output, usage


@cache
def get_agent() -> Agent[ParsedEmail, ExtractionResult]:
    """The agent, built once from the configured provider/model."""
    model, model_settings = build_model()
    agent = Agent(
        model,
        deps_type=ParsedEmail,
        output_type=NativeOutput(ExtractionResult),
        instructions=SYSTEM_PROMPT,
        model_settings=model_settings,
        toolsets=[erp_read_only_toolset()],
        retries={"output": 2, "tools": 1},
    )

    @agent.output_validator
    def validate(ctx: RunContext[ParsedEmail], output: ExtractionResult) -> ExtractionResult:
        problems = hallucinations(output, ctx.deps.normalized_text)
        if not problems:
            return output
        if ctx.retry >= ctx.max_retries:
            # Out of retries: accept it; the code gate will refuse to write what fails.
            log.warning("Output still has problems after retries", extra={"problems": problems})
            return output
        log.info("Asking the model to correct its output", extra={"problems": problems})
        raise ModelRetry("Please fix these problems and answer again:\n- " + "\n- ".join(problems))

    return agent


def hallucinations(output: ExtractionResult, email_text: str) -> list[str]:
    """Quotes, PO numbers and part numbers in the answer that aren't in the email.

    Only these go back to the model. A real conflict in the email (e.g. a part that isn't
    on the PO it names) is not a model mistake: the eval showed that asking the model to
    "fix" one made it drop the PO. The code gate reports such conflicts instead.
    """
    haystack = normalize_for_match(email_text)
    problems = [
        f'This evidence quote is not in the email; copy the exact text instead: "{quote}"'
        for c in output.commitments
        for quote in c.evidence
        if normalize_for_match(quote) not in haystack
    ]
    for c in output.commitments:
        for kind, value in (("PO", c.po_number), ("Part", c.part_number)):
            if value and not mentioned(value, email_text):
                problems.append(
                    f"{kind} {value} is not mentioned in the email. Use only {kind.lower()} "
                    "numbers the email states, or null if it states none."
                )
    return problems


def build_model() -> tuple[Model, ModelSettings]:
    """The model and settings configured by LLM_PROVIDER / LLM_MODEL / LLM_EFFORT."""
    if settings.llm_provider == "openai":
        if not settings.llm_model:
            raise ValueError("Set LLM_MODEL to an OpenAI model name when LLM_PROVIDER=openai")
        model = OpenAIChatModel(
            settings.llm_model, provider=OpenAIProvider(api_key=settings.openai_api_key)
        )
    else:
        model = AnthropicModel(
            settings.llm_model or DEFAULT_ANTHROPIC_MODEL,
            provider=AnthropicProvider(api_key=settings.anthropic_api_key),
        )
    return model, build_model_settings()


def build_model_settings() -> ModelSettings:
    """Request settings for the configured provider (no client or API key needed)."""
    if settings.llm_provider == "openai":
        # Effort maps to reasoning_effort; only sent if set (non-reasoning models reject it).
        extra = {"thinking": settings.llm_effort} if settings.llm_effort else {}
        return ModelSettings(max_tokens=MAX_OUTPUT_TOKENS, **extra)
    return AnthropicModelSettings(
        max_tokens=MAX_OUTPUT_TOKENS,
        thinking=settings.llm_effort or "medium",  # sent as Anthropic's effort setting
        # If a safety classifier declines, the API retries the request on a fallback model.
        anthropic_betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
    )


def erp_read_only_toolset() -> AbstractToolset:
    """The ERP's MCP server, filtered to the read-only tools (an allowlist: fails closed).

    A tool error (e.g. "PO not found") is shown to the model as a failed result it can
    adapt to; it doesn't use up the retry budget.
    """
    erp = MCPToolset(erp_mcp_target(), tool_error_behavior="failed")
    return erp.filtered(lambda ctx, tool: tool.name in READ_ONLY_ERP_TOOLS)

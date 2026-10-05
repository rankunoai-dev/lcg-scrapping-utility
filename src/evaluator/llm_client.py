"""S6 — thin Anthropic & OpenRouter API wrapper for semantic evaluation.

Supports both direct Anthropic API keys (sk-ant-...) and OpenRouter keys (sk-or-v1-...).

Credit Safety Guardrails:
  * Session Cost Cap ($1.00 USD default for test runs, configurable via MAX_COST_USD or --max-cost).
  * Auto-mapping of OpenRouter model IDs (e.g. anthropic/claude-haiku-4.5).
  * Schema-validated structured outputs (pydantic parsing).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-20241022"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"


class LLMUnavailable(Exception):
    """Credentials missing, cost limit reached, repeated API failure, or schema failure."""


@dataclass
class LLMCallMeta:
    model: str = ""
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    attempts: int = 1
    stop_reason: str | None = None
    flags: list[str] = field(default_factory=list)


class LLMClient:
    def __init__(self, model: str | None = None, schema_retries: int = 2,
                 max_tokens: int = 8000, max_cost_usd: float | None = None):
        self.schema_retries = schema_retries
        self.max_tokens = max_tokens
        
        # Load API keys & determine provider
        self.openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

        # Handle sk-or-v1- key in ANTHROPIC_API_KEY if passed there
        if not self.openrouter_key and self.anthropic_key.startswith("sk-or-v1-"):
            self.openrouter_key = self.anthropic_key

        self.is_openrouter = bool(self.openrouter_key)
        
        if model:
            self.model = model
        elif self.is_openrouter:
            self.model = os.environ.get("LCG_LLM_MODEL", DEFAULT_OPENROUTER_MODEL)
        else:
            self.model = os.environ.get("LCG_LLM_MODEL", DEFAULT_ANTHROPIC_MODEL)

        # Safety Cost Cap (Default $1.00 USD per run unless overridden)
        env_cap = os.environ.get("DEFAULT_MAX_COST_USD")
        self.max_cost_usd = max_cost_usd if max_cost_usd is not None else (
            float(env_cap) if env_cap else 1.00
        )
        self.total_cost_usd = 0.0
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            if self.is_openrouter:
                from openai import OpenAI
                self._client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=self.openrouter_key,
                    default_headers={
                        "HTTP-Referer": "https://github.com/RankUno/lcg-utility",
                        "X-Title": "LCG Local SEO Audit Utility",
                    }
                )
            else:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.anthropic_key)
        return self._client

    def _check_cost_cap(self, in_tokens: int, out_tokens: int):
        # $/MTok — verified against the OpenRouter models endpoint 2026-10-05
        # (anthropic/claude-haiku-4.5: $1.00 in / $5.00 out). Unknown models
        # use a conservative Sonnet-class estimate.
        m = self.model.lower()
        if "haiku" in m:
            pin, pout = 1.00, 5.00
        elif "opus-5.5" in m or "opus-5-5" in m:
            pin, pout = 4.00, 20.00
        elif "opus" in m:
            pin, pout = 5.00, 25.00
        elif "fable" in m:
            pin, pout = 10.00, 50.00
        else:
            pin, pout = 3.00, 15.00

        call_cost = (in_tokens / 1e6 * pin) + (out_tokens / 1e6 * pout)
        self.total_cost_usd += call_cost

        if self.max_cost_usd > 0 and self.total_cost_usd >= self.max_cost_usd:
            msg = (f"Credit Safety Cap Reached! Session cost is ${self.total_cost_usd:.4f} USD "
                   f"(Limit: ${self.max_cost_usd:.2f} USD). Halting further LLM calls to prevent unexpected charges. "
                   f"Pass --max-cost or --all-schools to expand the safety limit.")
            logger.warning(msg)
            raise LLMUnavailable(msg)

    def evaluate(self, system_prompt: str, user_payload: str, schema):
        """Returns (schema instance, LLMCallMeta). Raises LLMUnavailable."""
        if self.max_cost_usd > 0 and self.total_cost_usd >= self.max_cost_usd:
            raise LLMUnavailable(f"Credit Safety Cap Reached (${self.total_cost_usd:.2f} USD)")

        if not self.openrouter_key and not self.anthropic_key:
            raise LLMUnavailable("No API credentials found (set OPENROUTER_API_KEY or ANTHROPIC_API_KEY in .env)")

        if self.is_openrouter:
            return self._evaluate_openrouter(system_prompt, user_payload, schema)
        else:
            return self._evaluate_anthropic(system_prompt, user_payload, schema)

    def _evaluate_openrouter(self, system_prompt: str, user_payload: str, schema):
        client = self._ensure_client()
        meta = LLMCallMeta(model=self.model)
        last_err: Exception | None = None

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ]

        for attempt in range(1, self.schema_retries + 2):
            meta.attempts = attempt
            try:
                response = client.beta.chat.completions.parse(
                    model=self.model,
                    messages=messages,
                    response_format=schema,
                )
                parsed = response.choices[0].message.parsed
                if parsed is None:
                    raise ValueError("parse() returned None output")

                usage = getattr(response, "usage", None)
                if usage:
                    meta.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    meta.output_tokens = getattr(usage, "completion_tokens", 0) or 0
                
                self._check_cost_cap(meta.input_tokens, meta.output_tokens)
                meta.request_id = getattr(response, "id", None)
                return parsed, meta

            except LLMUnavailable:
                raise
            except Exception as e:
                last_err = e
                meta.flags.append(f"SCHEMA_RETRY_{attempt}")
                logger.warning("OpenRouter evaluation attempt %d failed: %s", attempt, e)

        raise LLMUnavailable(f"OpenRouter LLM call failed after {meta.attempts} attempts: {last_err}")

    def _evaluate_anthropic(self, system_prompt: str, user_payload: str, schema):
        import anthropic
        client = self._ensure_client()
        meta = LLMCallMeta(model=self.model)
        last_err: Exception | None = None

        for attempt in range(1, self.schema_retries + 2):
            meta.attempts = attempt
            try:
                response = client.messages.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_payload}],
                    output_format=schema,
                )
                meta.request_id = getattr(response, "_request_id", None)
                meta.stop_reason = getattr(response, "stop_reason", None)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    meta.input_tokens = getattr(usage, "input_tokens", 0) or 0
                    meta.output_tokens = getattr(usage, "output_tokens", 0) or 0
                    meta.cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

                self._check_cost_cap(meta.input_tokens, meta.output_tokens)

                if meta.stop_reason == "refusal":
                    raise LLMUnavailable("Model declined the request (stop_reason=refusal)")
                parsed = getattr(response, "parsed_output", None)
                if parsed is None:
                    last_err = ValueError("parse() returned no parsed_output")
                    meta.flags.append(f"SCHEMA_RETRY_{attempt}")
                    continue
                return parsed, meta

            except LLMUnavailable:
                raise
            except anthropic.BadRequestError as e:
                raise LLMUnavailable(f"BadRequest from API: {e.message}") from e
            except anthropic.AuthenticationError as e:
                raise LLMUnavailable("Invalid Anthropic API key") from e
            except Exception as e:
                last_err = e
                meta.flags.append(f"SCHEMA_RETRY_{attempt}")
                logger.warning("Anthropic API attempt %d failed: %s", attempt, e)

        raise LLMUnavailable(f"Anthropic LLM call failed after {meta.attempts} attempts: {last_err}")


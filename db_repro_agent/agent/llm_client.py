"""SDK-backed LLM clients for structured ActionPlan generation."""

from __future__ import annotations

import json
from typing import Any, Protocol

from openai import APIError, APIStatusError, OpenAI

from ..config import LLMConfig


class LLMClient(Protocol):
    def generate_action(self, context: dict[str, Any]) -> Any:
        """Return untrusted JSON representing an ActionPlan or Action."""


class LLMClientError(RuntimeError):
    """Raised when the configured LLM cannot return structured JSON."""


class OpenAICompatibleClient:
    """Use the OpenAI Python SDK with Responses-first fallback behavior.

    Responses is the default. Chat Completions is attempted only when the
    gateway explicitly appears not to support the Responses endpoint.
    """

    _SYSTEM_PROMPT = (
        "You generate exactly one executable database Action. "
        "Return JSON only, with an action object using one of the supported "
        "types: sql, transaction, benchbase, chaosblade. "
        "Never silently repair or rewrite an action after generation."
    )

    def __init__(self, config: LLMConfig) -> None:
        if not config.base_url or not config.api_key or not config.model:
            raise ValueError("LLM base_url, api_key and model are required")
        self.base_url = config.base_url.rstrip("/")
        self.api_key = config.api_key
        self.model = config.model
        self.api_mode = config.api_mode
        self.timeout_seconds = config.request_timeout_seconds
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    def generate_action(self, context: dict[str, Any]) -> dict[str, Any]:
        if self.api_mode == "chat_completions":
            return self._generate_chat(context)
        try:
            return self._generate_responses(context)
        except Exception as exc:
            if not self._is_responses_unsupported(exc):
                raise LLMClientError(f"Responses request failed: {exc}") from exc
            try:
                return self._generate_chat(context)
            except Exception as chat_exc:
                raise LLMClientError(
                    f"Responses unsupported and Chat Completions fallback failed: {chat_exc}"
                ) from chat_exc

    def _generate_responses(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self.client.responses.create(
            model=self.model,
            instructions=self._SYSTEM_PROMPT,
            input=json.dumps(context, ensure_ascii=False),
            text={"format": {"type": "json_object"}},
        )
        content = getattr(response, "output_text", None)
        if not isinstance(content, str):
            raise LLMClientError("Responses result has no output_text")
        return self._parse_json(content)

    def _generate_chat(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        )
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMClientError("Chat response has no choices[0].message.content") from exc
        if not isinstance(content, str):
            raise LLMClientError("Chat message content must be a JSON string")
        return self._parse_json(content)

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"LLM content is not valid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise LLMClientError("LLM JSON output must be an object")
        return result

    @staticmethod
    def _is_responses_unsupported(exc: Exception) -> bool:
        if isinstance(exc, APIStatusError) and exc.status_code in {400, 404, 405, 415, 422}:
            return True
        if isinstance(exc, APIError):
            message = str(exc).lower()
            return any(
                token in message
                for token in ("responses", "unsupported", "not found", "unknown endpoint")
            )
        return False

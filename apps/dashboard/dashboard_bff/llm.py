"""LLM provider abstraction, called only from the BFF (server side).

`ChatProvider` is the interface the agent uses; `OpenAICompatibleProvider`
implements it for any OpenAI-compatible Chat Completions API with function
calling. Configuration comes from env (Helm: ConfigMap + Secret). With no
provider configured, `provider_from_env()` returns None and chat reports a
configuration error; nothing is simulated.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """kind: not_configured | timeout | rate_limited | auth | context_length | bad_request | unavailable | malformed"""

    def __init__(self, kind: str, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model; validated before use


@dataclass
class ProviderReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class ChatProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderReply: ...


_SECRETISH = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+\S+|api[_-]?key\S*)")


def _safe(text: str, limit: int = 200) -> str:
    return _SECRETISH.sub("***", text.replace("\n", " "))[:limit]


class OpenAICompatibleProvider:
    def __init__(self, *, base_url: str, model: str, api_key: str | None, timeout: float = 60.0,
                 max_tokens: int = 1024, transport: httpx.AsyncBaseTransport | None = None):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ProviderReply:
        body: dict[str, Any] = {"model": self._model, "messages": messages, "max_tokens": self._max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=5), follow_redirects=False,
                                         trust_env=False, transport=self._transport) as http:
                r = await http.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError("timeout", f"The language model did not respond within {self._timeout:g}s.") from None
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"The language model provider is unreachable ({type(exc).__name__}).") from None

        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            raise ProviderError("rate_limited", "The language model provider is rate limiting requests. Try again shortly.",
                                retry_after=float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if r.status_code in (401, 403):
            raise ProviderError("auth", "The language model provider rejected the server's credentials (operator configuration).")
        detail = ""
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            detail = str(data["error"].get("message") or data["error"].get("code") or "")
        if r.status_code in (400, 413, 422):
            if re.search(r"(?i)context.length|maximum context|too many tokens|context window|prompt is too long", detail):
                raise ProviderError("context_length", "The conversation is too long for the model.")
            raise ProviderError("bad_request", f"The language model rejected the request: {_safe(detail) or r.status_code}")
        if r.status_code >= 500:
            raise ProviderError("unavailable", f"The language model provider failed (HTTP {r.status_code}).")
        if r.status_code != 200 or not isinstance(data, dict):
            raise ProviderError("malformed", f"Unexpected response from the language model provider (HTTP {r.status_code}).")

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("malformed", "The language model returned no message.") from None
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            calls.append(ToolCall(id=str(tc.get("id") or f"call_{i}"), name=str(fn.get("name") or ""),
                                  arguments=fn.get("arguments") if isinstance(fn.get("arguments"), str)
                                  else json.dumps(fn.get("arguments") or {})))
        content = msg.get("content")
        return ProviderReply(content=content if isinstance(content, str) else None, tool_calls=calls)


def provider_from_env(env: dict[str, str] | None = None) -> ChatProvider | None:
    env = dict(os.environ if env is None else env)
    base = env.get("CHAT_PROVIDER_BASE_URL", "").strip()
    model = env.get("CHAT_MODEL", "").strip()
    if not base or not model:
        return None
    if not base.startswith("https://") and not base.startswith("http://localhost"):
        raise SystemExit("CHAT_PROVIDER_BASE_URL must be https:// (or http://localhost for development)")
    timeout = float(env.get("CHAT_PROVIDER_TIMEOUT_SECONDS", "60"))
    max_tokens = int(env.get("CHAT_MAX_OUTPUT_TOKENS", "1024"))
    return OpenAICompatibleProvider(base_url=base, model=model, api_key=env.get("CHAT_PROVIDER_API_KEY") or None,
                                    timeout=timeout, max_tokens=max_tokens)

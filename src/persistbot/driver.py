"""Model calls. One entry point, `call`, dispatched on the resolved driver.

The SDK never retries here: an upstream error is raised as one of two
classes, and the tick decides what to do with the goal.

  UpstreamRetryable  408, 409, 429, 5xx, timeouts, connection failures.
  UpstreamFatal      any other failure: a bad key, a bad request, a reply
                     that cannot be read. An operator has to look.

Neither is the model's fault, so neither counts against a goal.
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import openai

from persistbot import log

if TYPE_CHECKING:
    from persistbot.persona import ResolvedModel

DEFAULT_TIMEOUT = 300.0
NO_KEY = "no-key"  # sent when the keys entry is empty; local servers ignore it
RETRYABLE_STATUS = {408, 409, 429}


class UpstreamError(Exception):
    """A model call failed for a reason outside the model's work."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UpstreamRetryable(UpstreamError):
    """Worth trying again later. `retry_after` is seconds, when the server said."""

    def __init__(
        self, message: str, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, status)
        self.retry_after = retry_after


class UpstreamFatal(UpstreamError):
    """Will not fix itself: wrong key, wrong model, bad request, unreadable reply."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # the JSON text exactly as the model wrote it


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None

    def message(self) -> dict[str, Any]:
        """This reply as the assistant message to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


def _retry_after(headers: Any) -> float | None:
    """Seconds from a Retry-After header: a number, or an HTTP date."""
    value = headers.get("retry-after") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _status_error(exc: openai.APIStatusError) -> UpstreamError:
    status = exc.status_code
    message = f"HTTP {status}: {exc.message}"
    if status in RETRYABLE_STATUS or status >= 500:
        return UpstreamRetryable(message, status, _retry_after(exc.response.headers))
    return UpstreamFatal(message, status)


def _openai(model: "ResolvedModel", messages: list[dict], tools: list[dict] | None) -> Reply:
    parameters = dict(model.parameters)
    timeout = parameters.pop("timeout", DEFAULT_TIMEOUT)
    request: dict[str, Any] = {"model": model.model, "messages": messages}
    if tools:
        request["tools"] = tools
    if parameters:
        request["extra_body"] = parameters  # passed through as given
    try:
        with openai.OpenAI(
            base_url=model.url,
            api_key=model.auth.get("key") or NO_KEY,
            timeout=timeout,
            max_retries=0,
        ) as client:
            response = client.chat.completions.create(**request)
    except openai.APITimeoutError as exc:
        raise UpstreamRetryable(f"timed out after {timeout:g}s") from exc
    except openai.APIConnectionError as exc:
        raise UpstreamRetryable(f"connection failed: {exc.__cause__ or exc}") from exc
    except openai.APIStatusError as exc:
        raise _status_error(exc) from exc
    except openai.OpenAIError as exc:
        raise UpstreamFatal(f"{type(exc).__name__}: {exc}") from exc

    try:
        choice = response.choices[0]
        calls = tuple(
            ToolCall(id=c.id or "", name=c.function.name or "", arguments=c.function.arguments or "")
            for c in (choice.message.tool_calls or [])
        )
        text = choice.message.content or ""
        finish = choice.finish_reason
    except (AttributeError, IndexError, TypeError) as exc:
        raise UpstreamFatal(f"unreadable reply: {type(exc).__name__}: {exc}") from exc
    usage = response.usage
    return Reply(
        text=text,
        tool_calls=calls,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        finish_reason=finish,
    )


DRIVERS: dict[str, Callable[["ResolvedModel", list[dict], list[dict] | None], Reply]] = {
    "openai": _openai,
}


def call(model: "ResolvedModel", messages: list[dict], tools: list[dict] | None = None) -> Reply:
    """Send one chat completion request. Raises UpstreamRetryable or UpstreamFatal."""
    started = time.monotonic()
    try:
        reply = DRIVERS[model.driver](model, messages, tools)
    except UpstreamError as exc:
        kind = "retryable" if isinstance(exc, UpstreamRetryable) else "fatal"
        log.action(
            f"model call as {model.name} ({model.provider}) failed, {kind}: {exc}",
            level="warning",
        )
        raise
    log.action(
        f"model call as {model.name} ({model.provider}) ok in "
        f"{time.monotonic() - started:.1f}s: {reply.prompt_tokens} in, "
        f"{reply.completion_tokens} out, {len(reply.tool_calls)} tool call(s)"
    )
    return reply

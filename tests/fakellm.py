"""A scripted stand-in for an OpenAI-compatible chat completions server.

Every test that needs a model uses this. It listens on 127.0.0.1, plays the
queued steps in order, one per request, and records what it was sent.

    with FakeLLM() as llm:
        llm.add(error(429, retry_after=30))
        llm.add(tool_call("run_shell", {"command": "ls"}))
        llm.add(text("All done."))
        ...point a provider's url at llm.url...
    llm.requests[0]["body"]["messages"]

A request that arrives after the script is spent gets HTTP 500 and is
counted in `llm.unscripted`, so a test can assert it never happened.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def text(content: str, finish: str = "stop", usage: tuple[int, int] = (10, 5)) -> dict:
    """A plain assistant reply."""
    return _completion({"role": "assistant", "content": content}, finish, usage)


def tool_call(name: str, arguments: Any, call_id: str = "call_1", content: str | None = None,
              usage: tuple[int, int] = (10, 5)) -> dict:
    """An assistant reply holding one tool call. `arguments` may be raw text."""
    return tool_calls([(name, arguments, call_id)], content, usage)


def tool_calls(calls: list[tuple], content: str | None = None,
               usage: tuple[int, int] = (10, 5)) -> dict:
    """An assistant reply holding several tool calls: (name, arguments[, id])."""
    items = []
    for index, call in enumerate(calls):
        name, arguments = call[0], call[1]
        call_id = call[2] if len(call) > 2 else f"call_{index + 1}"
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        items.append(
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        )
    message = {"role": "assistant", "content": content, "tool_calls": items}
    return _completion(message, "tool_calls", usage)


def error(status: int, message: str = "scripted error", retry_after: Any = None) -> dict:
    """An HTTP error in the OpenAI error shape."""
    headers = {} if retry_after is None else {"Retry-After": str(retry_after)}
    body = {"error": {"message": message, "type": "scripted", "code": status}}
    return {"status": status, "body": body, "headers": headers}


def drop() -> dict:
    """Close the connection without answering."""
    return {"drop": True}


def slow(step: dict, seconds: float) -> dict:
    """Delay `step` by `seconds` before it is sent."""
    return {**step, "delay": seconds}


def raw(status: int, body: str, content_type: str = "text/plain") -> dict:
    """Any status with a body that is not a chat completion."""
    return {"status": status, "raw": body, "headers": {"Content-Type": content_type}}


def _completion(message: dict, finish: str, usage: tuple[int, int]) -> dict:
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }
    return {"status": 200, "body": body, "headers": {}}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # keep test output clean
        pass

    def do_POST(self) -> None:
        fake: FakeLLM = self.server.fake  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length)
        try:
            body = json.loads(payload) if payload else None
        except ValueError:
            body = payload.decode("utf-8", "replace")
        step = fake._next(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
             "body": body}
        )
        if step.get("delay"):
            time.sleep(step["delay"])
        if step.get("drop"):
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if "raw" in step:
            data = step["raw"].encode("utf-8")
        else:
            data = json.dumps(step["body"]).encode("utf-8")
        headers = {"Content-Type": "application/json", **step.get("headers", {})}
        try:
            self.send_response(step["status"])
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:  # the client gave up, as in a timeout test
            self.close_connection = True


class FakeLLM:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.unscripted = 0
        self._stopped = False
        self._steps: list[dict] = []
        self._guard = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        """Base URL to put in a provider or persona entry."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    @property
    def remaining(self) -> int:
        with self._guard:
            return len(self._steps)

    def add(self, *steps: dict) -> "FakeLLM":
        with self._guard:
            self._steps.extend(steps)
        return self

    def _next(self, request: dict) -> dict:
        with self._guard:
            self.requests.append(request)
            if self._steps:
                return self._steps.pop(0)
            self.unscripted += 1
        return error(500, "fakellm: no scripted step left for this request")

    def start(self) -> "FakeLLM":
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop listening. Safe to call twice; the port then refuses connections."""
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeLLM":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

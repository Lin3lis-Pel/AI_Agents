"""Gives every HTTP request an id, logs its outcome and turns a crash into a clean 500.

The id is made here, never taken from the client, so it cannot be used to write text
into the log. It is sent back as `X-Request-ID`, and the review service logs it next to
the review id, so one request can be followed from the browser to the failing node.

Plain ASGI, not Starlette's `BaseHTTPMiddleware`: a review is a streamed response.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from tools.logs import log_event, log_failure

HEADER = "X-Request-ID"
ASGIApp = Callable[..., Awaitable[None]]


class RequestLog:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:12]
        scope.setdefault("state", {})["request_id"] = request_id
        started, status = time.perf_counter(), 0

        async def send_with_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                id_header = (HEADER.lower().encode(), request_id.encode())
                message = {**message, "headers": [*message.get("headers", []), id_header]}
            await send(message)

        try:
            await self._app(scope, receive, send_with_id)
        except Exception as exc:
            log_failure("request.failed", exc, request_id=request_id, method=scope["method"])
            if status:
                raise  # the answer has begun: the server can only cut the connection
            await _send_500(send_with_id, request_id)
        finally:
            log_event(
                "request.finished",
                level=logging.INFO if status < 500 else logging.ERROR,
                request_id=request_id,
                method=scope["method"],
                route=_route(scope),
                status=status or None,
                seconds=round(time.perf_counter() - started, 4),
            )


def _route(scope: dict[str, Any]) -> str:
    """The route's pattern, not the path: a path can contain anything."""
    return getattr(scope.get("route"), "path", "(no route)")


async def _send_500(send: Any, request_id: str) -> None:
    body = json.dumps({"detail": f"Internal error. Reference: {request_id}."}).encode()
    headers = [(b"content-type", b"application/json")]
    await send({"type": "http.response.start", "status": 500, "headers": headers})
    await send({"type": "http.response.body", "body": body})

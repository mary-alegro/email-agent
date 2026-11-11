"""Request logging middleware: assigns a request_id and logs one line per request.

Written as plain ASGI middleware (not BaseHTTPMiddleware) so it doesn't buffer the
streaming responses the MCP endpoint uses.
"""

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.utils.logging import request_id_var

log = logging.getLogger("app.requests")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QUIET_PATHS = {"/health"}  # logged at DEBUG to keep health checks out of INFO logs


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex[:12]
        request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            log.exception("Unhandled error in %s %s", scope["method"], scope["path"])
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000)
            level = logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO
            log.log(
                level,
                "%s %s",
                scope["method"],
                scope["path"],
                extra={"status": status, "duration_ms": duration_ms},
            )


def _incoming_request_id(scope: Scope) -> str | None:
    """Reuse a caller's X-Request-ID (if it looks sane) so IDs line up across services."""
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None

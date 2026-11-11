"""Map errors to HTTP responses."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.erp.errors import NotFoundError
from src.utils.logging import request_id_var


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        # The traceback is already logged by RequestLoggingMiddleware. The caller gets
        # the request_id to quote in a bug report, never internal details.
        request_id = request_id_var.get()
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

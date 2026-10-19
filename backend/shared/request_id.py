"""
Request ID middleware helper.
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import FastAPI, Request

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def install_request_id_middleware(app: FastAPI) -> None:
    """Attach request-id middleware to a FastAPI app."""

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        supplied = (request.headers.get("X-Request-ID") or "").strip()
        request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

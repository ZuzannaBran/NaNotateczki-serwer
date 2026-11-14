import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .database import engine
from .routers import (
    account_settings, assets, auth, backups, operation_realtime, operations,
    planner_v2, reports, study_shares,
)
from .settings import get_settings
from .browser_origins import localhost_origin_regex


settings = get_settings()
app = FastAPI(
    title="NaNotateczki API",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(settings.app_origin).rstrip("/")],
    allow_origin_regex=localhost_origin_regex(settings),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Content-SHA256",
        "X-Share-Id",
        "X-Request-ID",
    ],
)


@app.middleware("http")
async def security_boundary(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", request_id):
        return JSONResponse(status_code=400, content={"detail": "Invalid request ID."})
    if request.method in {"POST", "PUT", "DELETE"}:
        length = request.headers.get("content-length")
        if length is None and request.method == "DELETE" and not request.headers.get("transfer-encoding"):
            length = "0"
        if length is None or not length.isdigit():
            return JSONResponse(
                status_code=411, content={"detail": "Content-Length is required."}
            )
        if request.url.path.startswith("/v2/assets/"):
            maximum = settings.maximum_asset_bytes
        elif request.url.path.startswith("/v2/backups/") and request.url.path.endswith(
            "/content"
        ):
            maximum = settings.maximum_backup_bytes
        else:
            maximum = 1024 * 1024
        if int(length) > maximum:
            return JSONResponse(status_code=413, content={"detail": "Request is too large."})
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "nanotateczki-api"}


@app.get("/v2/time")
async def server_time() -> dict:
    # Unix UTC epoch milliseconds, independent of the server's timezone.
    # The client compares this against its monotonic request midpoint.
    return {"serverTimeMs": time.time_ns() // 1_000_000}


@app.get("/ready")
async def ready() -> dict:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"status": "ready"}


app.include_router(auth.router)
app.include_router(account_settings.v2_router)
app.include_router(planner_v2.router)
app.include_router(backups.v2_router)
app.include_router(operations.router)
app.include_router(assets.router)
app.include_router(operation_realtime.router)
app.include_router(study_shares.router)
app.include_router(reports.router)

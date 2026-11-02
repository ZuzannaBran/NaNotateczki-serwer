import re
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .database import engine
from .routers import account_settings, auth, backups, planner, realtime, reports, study_shares, sync
from .settings import get_settings


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
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Content-SHA256",
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
        if request.url.path.startswith("/v1/study-shares/") and request.url.path.endswith("/document"):
            maximum = settings.maximum_sync_bytes
        elif request.url.path.startswith("/v1/study-shares/") and request.url.path.endswith("/frame"):
            maximum = 2 * 1024 * 1024
        elif request.url.path.startswith("/v1/sync/notebooks/"):
            maximum = settings.maximum_sync_bytes
        elif request.url.path.startswith("/v1/backups/") and request.url.path.endswith(
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


@app.get("/ready")
async def ready() -> dict:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"status": "ready"}


app.include_router(auth.router)
app.include_router(account_settings.router)
app.include_router(planner.router)
app.include_router(backups.router)
app.include_router(sync.router)
app.include_router(realtime.router)
app.include_router(study_shares.router)
app.include_router(reports.router)

"""Verified v2 asset upload/download scoped to the account or share."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..account_protocol import require_v2_protocol
from ..asset_store import asset_path, store_upload, valid_verified_file
from ..database import database_session
from ..models import DocumentOperation, DocumentState, VerifiedAsset
from ..settings import get_settings
from .operations import authorize, check_share_lock, require_operations_v2_write

router = APIRouter(prefix="/v2/assets", tags=["assets"])


async def principal_for_asset(
    session: AsyncSession, authorization: str | None,
    share_id: uuid.UUID | None,
):
    principal = await authorize(session, authorization, share_id)
    require_operations_v2_write(principal.owner_id)
    await require_v2_protocol(session, principal.owner_id)
    await check_share_lock(session, principal)
    return principal


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise HTTPException(status_code=422, detail="Invalid asset SHA-256.")


@router.put("/{sha256}")
async def upload_asset(
    sha256: str,
    request: Request,
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
) -> dict:
    _validate_digest(sha256)
    principal = await principal_for_asset(session, authorization, share_id)
    settings = get_settings()
    size, mime = await store_upload(
        request, settings.data_path, principal.owner_id,
        sha256, settings.maximum_asset_bytes,
    )
    original = await session.get(VerifiedAsset, (principal.owner_id, sha256))
    if original is not None and (
        original.size_bytes != size or original.media_type != mime
    ):
        raise HTTPException(status_code=409, detail="Asset identity conflict.")
    if original is None:
        session.add(VerifiedAsset(
            owner_id=principal.owner_id, sha256=sha256,
            size_bytes=size, media_type=mime,
        ))
    await session.commit()
    return {"sha256": sha256, "sizeBytes": size, "mediaType": mime}


def historical_share_asset_query(
    owner_id: uuid.UUID, document_uid: str, sha256: str,
):
    """A scoped historical attachment, never a global asset lookup."""
    return (
        select(DocumentOperation.cursor)
        .where(
            DocumentOperation.owner_id == owner_id,
            DocumentOperation.document_uid == document_uid,
            DocumentOperation.op_kind == "asset.attach",
            DocumentOperation.envelope["payload"]["sha256"].astext == sha256,
        )
        .limit(1)
    )


@router.get("/{sha256}")
async def download_asset(
    sha256: str,
    authorization: str | None = Header(default=None),
    share_id: uuid.UUID | None = Header(default=None, alias="X-Share-Id"),
    session: AsyncSession = Depends(database_session),
):
    _validate_digest(sha256)
    principal = await principal_for_asset(session, authorization, share_id)
    record = await session.get(VerifiedAsset, (principal.owner_id, sha256))
    if record is None:
        raise HTTPException(status_code=404, detail="Asset not found.")
    if principal.share_id is not None:
        document = await session.get(
            DocumentState, (principal.owner_id, principal.document_uid)
        )
        pages = (document.content or {}).get("pages", {}) if document else {}
        referenced = any(
            asset.get("sha256") == sha256
            for page in pages.values()
            for node in page.get("objects", {}).values()
            for asset in node.get("assets", {}).values()
        )
        if not referenced:
            # Guests replay the whole visible operation feed. A formerly
            # attached asset is already disclosed by that feed even when
            # later detached. Replay must not fail solely because the
            # current materialized document no longer references it.
            historical = await session.scalar(
                historical_share_asset_query(
                    principal.owner_id, principal.document_uid, sha256,
                )
            )
            if historical is None:
                raise HTTPException(
                    status_code=403, detail="Share scope violation."
                )
    path = asset_path(get_settings().data_path, principal.owner_id, sha256)
    if not valid_verified_file(path, sha256, record.size_bytes):
        raise HTTPException(status_code=503, detail="Asset bytes not durable.")
    return FileResponse(
        path, media_type=record.media_type,
        headers={"ETag": sha256, "Cache-Control": "private, no-store"},
    )

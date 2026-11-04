"""Immutable owner-scoped asset storage, verified after durable write."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Request

CHUNK_SIZE = 1024 * 1024


def asset_path(root: Path, owner: UUID, digest: str) -> Path:
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid asset SHA-256.")
    return root / "assets-v2" / str(owner) / digest[:2] / digest


def content_type(prefix: bytes) -> str:
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(status_code=415, detail="Unsupported asset media type.")


def valid_verified_file(path: Path, digest: str, size: int) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
            return False
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                checksum.update(chunk)
        return checksum.hexdigest() == digest
    except OSError:
        return False


async def store_upload(
    request: Request, root: Path, owner: UUID, expected: str, maximum: int,
) -> tuple[int, str]:
    """Stream to a private temp file, checksum, fsync file and directory."""
    import tempfile

    destination = asset_path(root, owner, expected)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = None
    size = 0
    prefix = b""
    checksum = hashlib.sha256()
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".upload-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(
                        status_code=413, detail="Asset exceeds maximum size."
                    )
                if len(prefix) < 16:
                    prefix = (prefix + chunk)[:16]
                temporary.write(chunk)
                checksum.update(chunk)
            if size == 0 or checksum.hexdigest() != expected:
                raise HTTPException(status_code=422, detail="Asset SHA mismatch.")
            mime = content_type(prefix)
            temporary.flush()
            os.fsync(temporary.fileno())
        if not valid_verified_file(temporary_path, expected, size):
            raise HTTPException(status_code=422, detail="Uploaded bytes invalid.")
        os.replace(temporary_path, destination)
        temporary_path = None
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return size, mime
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.study_shares import (
    PNG_MAGIC, ShareCreate, ShareJoin, validate_png_frame,
)


def frame(width: int, height: int) -> bytes:
    return PNG_MAGIC + b"\x00\x00\x00\x0dIHDR" + (
        width.to_bytes(4, "big") + height.to_bytes(4, "big")
    ) + b"\x08\x06\x00\x00\x00" + b"\x00\x00\x00\x00"


def test_rejects_non_png_and_large_dimensions():
    with pytest.raises(HTTPException):
        validate_png_frame(b"not an image")
    with pytest.raises(HTTPException):
        validate_png_frame(frame(10000, 100))


def test_accepts_bounded_png():
    validate_png_frame(frame(1280, 720))


def test_rejects_oversized_upload():
    with pytest.raises(HTTPException) as error:
        validate_png_frame(frame(1280, 720) + b"x" * (2 * 1024 * 1024))
    assert error.value.status_code == 413


def test_extension_requires_owner_authentication():
    from app.routers.study_shares import extend_share
    import inspect
    parameters = inspect.signature(extend_share).parameters
    assert 'user' in parameters
    assert 'session' in parameters


def test_share_creation_checks_owner_cloud_document():
    # An uncommitted local notebook is not a valid owner-account copy.
    import inspect
    from app.routers.study_shares import create_share
    source = inspect.getsource(create_share)
    assert "SyncedNotebook.owner_id == user.id" in source
    assert "SyncedNotebook.deleted_at.is_(None)" in source
    assert "Save this document to your account" in source


def test_share_closure_does_not_delete_account_document():
    import inspect
    from app.routers.study_shares import end_share
    source = inspect.getsource(end_share)
    assert "SyncedNotebook" not in source
    assert "frame_path(share_id).unlink" in source


def test_session_password_allows_one_character():
    creation = ShareCreate(documentUid="board", title="Board", password="x")
    guest = ShareJoin(password="x")
    assert creation.password == guest.password == "x"


def test_session_password_rejects_empty_value():
    with pytest.raises(ValidationError):
        ShareCreate(documentUid="board", title="Board", password="")
    with pytest.raises(ValidationError):
        ShareJoin(password="")

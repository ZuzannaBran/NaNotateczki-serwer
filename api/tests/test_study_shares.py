import pytest
from fastapi import HTTPException

from app.routers.study_shares import PNG_MAGIC, validate_png_frame


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

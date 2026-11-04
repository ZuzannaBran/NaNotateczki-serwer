"""Stage-13 canary gate: no accidental v2 writes before migration."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import operations
from app.settings import Settings


@pytest.mark.parametrize(
    ("mode", "canaries", "allowed"),
    [
        ("disabled", "", False),
        ("disabled", None, False),
        ("canary", "", False),
        ("canary", "other", False),
        ("canary", "owner", True),
        ("all", "", True),
    ],
)
def test_v2_writes_require_explicit_rollout(monkeypatch, mode, canaries, allowed):
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    if canaries == "owner":
        ids = str(owner_id)
    elif canaries == "other":
        ids = str(other_id)
    else:
        ids = ""
    monkeypatch.setattr(
        operations,
        "get_settings",
        lambda: SimpleNamespace(
            operations_v2_write_mode=mode,
            operations_v2_canary_owner_ids=ids,
        ),
    )
    if allowed:
        operations.require_operations_v2_write(owner_id)
    else:
        with pytest.raises(HTTPException) as error:
            operations.require_operations_v2_write(owner_id)
        assert error.value.status_code == 403


def test_v2_rollout_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OPERATIONS_V2_WRITE_MODE", raising=False)
    monkeypatch.delenv("OPERATIONS_V2_CANARY_OWNER_IDS", raising=False)
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        APP_ORIGIN="https://app.example.test",
        JWT_SECRET="a" * 64,
    )
    assert settings.operations_v2_write_mode == "disabled"


def test_canary_uuid_configuration_must_be_valid(monkeypatch):
    monkeypatch.delenv("OPERATIONS_V2_CANARY_OWNER_IDS", raising=False)
    opts = {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "APP_ORIGIN": "https://app.example.test",
        "JWT_SECRET": "a" * 64,
    }
    with pytest.raises(ValidationError):
        Settings(**opts, OPERATIONS_V2_CANARY_OWNER_IDS="not-a-uuid")
    owner = uuid.uuid4()
    config = Settings(**opts, OPERATIONS_V2_CANARY_OWNER_IDS=f" {owner} ")
    assert config.operations_v2_canary_owner_ids == str(owner)

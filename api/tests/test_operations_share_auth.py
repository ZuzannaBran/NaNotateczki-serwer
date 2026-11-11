"""Expiry and revocation semantics for v2 Share operation credentials."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import StudyShare
from app.routers.operations import authorize


class _ShareSession:
    def __init__(self, share, grant):
        self.share = share
        self.grant = grant

    async def scalar(self, _query):
        return self.grant

    async def get(self, model, _key):
        assert model is StudyShare
        return self.share


def _objects(*, grant_expired=False, share_expired=False, revoked=False):
    now = datetime.now(UTC)
    share_id = uuid.uuid4()
    share = SimpleNamespace(
        id=share_id,
        owner_id=uuid.uuid4(),
        document_uid="document-1",
        expires_at=now + timedelta(hours=-1 if share_expired else 1),
        revoked_at=now if revoked else None,
        edit_clock={"_protocol": "v2"},
    )
    grant = SimpleNamespace(
        id=uuid.uuid4(),
        share_id=share_id,
        expires_at=now + timedelta(hours=-1 if grant_expired else 1),
    )
    return share, grant


@pytest.mark.parametrize(
    "options",
    [
        {"grant_expired": True},
        {"share_expired": True},
        {"revoked": True},
    ],
)
def test_valid_but_expired_or_revoked_ticket_is_terminal(options):
    share, grant = _objects(**options)
    with pytest.raises(HTTPException) as error:
        asyncio.run(authorize(
            _ShareSession(share, grant), "Bearer " + "s" * 40, share.id,
        ))
    assert error.value.status_code == 410


def test_unknown_ticket_remains_unauthorized():
    share, _ = _objects()
    with pytest.raises(HTTPException) as error:
        asyncio.run(authorize(
            _ShareSession(share, None), "Bearer " + "s" * 40, share.id,
        ))
    assert error.value.status_code == 401


def test_live_share_ticket_grants_only_its_document():
    share, grant = _objects()
    principal = asyncio.run(authorize(
        _ShareSession(share, grant), "Bearer " + "s" * 40, share.id,
    ))
    assert principal.kind == "share"
    assert principal.share_id == share.id
    assert principal.document_uid == share.document_uid
    assert principal.principal_id == grant.id


def test_legacy_share_ticket_never_authorizes_operational_writes():
    share, grant = _objects()
    share.edit_clock = {}
    with pytest.raises(HTTPException) as error:
        asyncio.run(authorize(
            _ShareSession(share, grant), "Bearer " + "s" * 40, share.id,
        ))
    assert error.value.status_code == 403

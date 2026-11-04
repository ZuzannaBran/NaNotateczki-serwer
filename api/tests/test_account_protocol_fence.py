"""Regression tests for the persisted account-wide v1/v2 write fence."""
import asyncio
import uuid

import pytest
from fastapi import HTTPException

from app.account_protocol import (
    account_protocol,
    require_legacy_protocol,
    require_v2_protocol,
)


class ProtocolSession:
    def __init__(self, mode):
        self.mode = mode
        self.queries = 0

    async def execute(self, query, params):
        self.queries += 1
        assert "account_sync_protocol" in str(query)
        assert isinstance(params["owner_id"], uuid.UUID)

        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one_or_none(self):
                return self.value

        return Result(self.mode)


def test_legacy_default_preserved_when_marker_missing():
    owner = uuid.uuid4()
    session = ProtocolSession(None)
    assert asyncio.run(account_protocol(session, owner)) == "v1"
    asyncio.run(require_legacy_protocol(session, owner))
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_v2_protocol(session, owner))
    assert error.value.status_code == 409


def test_activated_account_rejects_any_legacy_uid_write():
    owner = uuid.uuid4()
    session = ProtocolSession("v2")
    asyncio.run(require_v2_protocol(session, owner))
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_legacy_protocol(session, owner))
    assert error.value.status_code == 409
    assert "update the client" in error.value.detail


def test_fence_is_scoped_per_account():
    owners = [uuid.uuid4(), uuid.uuid4()]
    legacy = ProtocolSession(None)
    activated = ProtocolSession("v2")
    asyncio.run(require_legacy_protocol(legacy, owners[0]))
    asyncio.run(require_v2_protocol(activated, owners[1]))
    assert legacy.queries == 1
    assert activated.queries == 1


def test_unrecognized_marker_fails_closed():
    owner = uuid.uuid4()
    session = ProtocolSession("invalid")
    with pytest.raises(HTTPException):
        asyncio.run(require_legacy_protocol(session, owner))
    with pytest.raises(HTTPException):
        asyncio.run(require_v2_protocol(session, owner))


def test_authenticated_protocol_probe_returns_account_mode():
    from types import SimpleNamespace

    from app.routers.auth import read_protocol

    owner = uuid.uuid4()
    user = SimpleNamespace(id=owner)
    for marker, expected in ((None, "v1"), ("v2", "v2")):
        session = ProtocolSession(marker)
        assert asyncio.run(read_protocol(user, session)) == {
            "protocol": expected,
        }
        assert session.queries == 1

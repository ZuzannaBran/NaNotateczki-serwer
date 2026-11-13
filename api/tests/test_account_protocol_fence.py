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


class ReadinessSession(ProtocolSession):
    def __init__(self, mode, legacy=False):
        super().__init__(mode)
        self.legacy = legacy

    async def execute(self, query, params):
        if "legacy_account_cutovers" in str(query):
            self.queries += 1

            class EmptyResult:
                def mappings(self):
                    return self

                def one_or_none(self):
                    return None

            return EmptyResult()
        if "synced_notebooks" not in str(query):
            return await super().execute(query, params)
        self.queries += 1
        assert isinstance(params["owner_id"], uuid.UUID)

        class Result:
            def scalar_one(inner_self):
                return self.legacy

        return Result()


def test_activation_readiness_rejects_v1_and_unconverted_v2():
    from app.account_protocol import require_v2_activation_ready

    owner = uuid.uuid4()
    for marker, legacy in ((None, False), ("v2", True)):
        with pytest.raises(HTTPException) as error:
            asyncio.run(require_v2_activation_ready(
                ReadinessSession(marker, legacy), owner,
            ))
        assert error.value.status_code == 409
    verified = ReadinessSession("v2")
    asyncio.run(require_v2_activation_ready(verified, owner))
    assert verified.queries == 3


def test_activation_endpoint_requires_marker_and_canary_allowlist(monkeypatch):
    from types import SimpleNamespace
    from app.routers import operations

    owner = uuid.uuid4()

    async def authorize(session, authorization, share_id):
        assert authorization == "Bearer test" and share_id is None
        return operations.Principal(owner, "owner", owner)

    monkeypatch.setattr(operations, "authorize", authorize)
    def config(allowed):
        return SimpleNamespace(
            operations_v2_write_mode="canary",
            operations_v2_canary_owner_ids=str(allowed),
        )
    monkeypatch.setattr(operations, "get_settings", lambda: config(owner))

    def check(session):
        return asyncio.run(operations.get_activation_readiness(
            authorization="Bearer test", session=session,
        ))

    assert check(ReadinessSession("v2")) == {
        "protocol": "v2", "writeEnabled": True, "version": 1,
    }
    for session in (ReadinessSession(None), ReadinessSession("v2", True)):
        with pytest.raises(HTTPException) as error:
            check(session)
        assert error.value.status_code == 409
    monkeypatch.setattr(
        operations, "get_settings", lambda: config(uuid.uuid4()),
    )
    with pytest.raises(HTTPException) as error:
        check(ReadinessSession("v2"))
    assert error.value.status_code == 403

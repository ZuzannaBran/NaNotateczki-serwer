import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from app.models import RefreshSession, User
from app.routers import auth
from app.routers.auth import change_password
from app.schemas import Credentials, PasswordChangeRequest, RegistrationCredentials
from app.security import (
    create_access_token,
    current_user,
    decode_access_token,
    hash_password,
    rotate_refresh_session,
    token_digest,
    verify_password,
)


def test_password_is_argon2_and_plaintext_is_not_stored() -> None:
    encoded = hash_password("correct horse battery staple")

    assert encoded.startswith("$argon2")
    assert "correct horse battery staple" not in encoded
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_password_lengths_match_the_flutter_account_client() -> None:
    email = "user@example.com"
    assert Credentials(email=email, password="x").password == "x"
    assert (
        RegistrationCredentials(email=email, password="12345678").password
        == "12345678"
    )
    assert (
        PasswordChangeRequest(
            currentPassword="old", newPassword="12345678"
        ).newPassword
        == "12345678"
    )

    with pytest.raises(ValidationError):
        RegistrationCredentials(email=email, password="1234567")
    with pytest.raises(ValidationError):
        RegistrationCredentials(email=email, password="x" * 129)
    with pytest.raises(ValidationError):
        PasswordChangeRequest(currentPassword="old", newPassword="1234567")
    with pytest.raises(ValidationError):
        PasswordChangeRequest(currentPassword="old", newPassword="x" * 129)


def test_registration_flushes_user_before_creating_refresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        user_flushed = False
        refresh_added = False

        def add(self, record: User | RefreshSession) -> None:
            if isinstance(record, RefreshSession):
                assert self.user_flushed
                self.refresh_added = True

        async def flush(self) -> None:
            self.user_flushed = True

        async def commit(self) -> None:
            assert self.refresh_added

    async def skip_rate_limit(request: object, email: str) -> None:
        pass

    monkeypatch.setattr(auth, "limit_auth", skip_rate_limit)
    monkeypatch.setattr(
        auth, "get_settings", lambda: type("Settings", (), {"allow_registration": True})()
    )
    session = Session()
    result = asyncio.run(
        auth.register(
            auth.RegistrationCredentials(
                email="user@example.com", password="12345678"
            ),
            None,
            session,
        )
    )
    assert result["email"] == "user@example.com"


def test_access_token_is_bound_to_user_and_type() -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, 1)

    assert expires_in == 600
    assert decode_access_token(token) == user_id

    with pytest.raises(HTTPException):
        decode_access_token(token + "tampered")


def test_old_access_token_is_rejected_after_password_version_changes() -> None:
    user = User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=hash_password("correct horse battery staple"),
        auth_version=1,
    )
    token, _ = create_access_token(user.id, user.auth_version)

    class Session:
        async def get(self, model: object, user_id: uuid.UUID) -> User:
            assert user_id == user.id
            return user

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    assert asyncio.run(current_user(credentials, Session())) is user

    user.auth_version = 2
    with pytest.raises(HTTPException) as error:
        asyncio.run(current_user(credentials, Session()))
    assert error.value.status_code == 401
    assert error.value.detail == "Session has expired. Sign in again."


def test_password_change_rehashes_and_revokes_refresh_sessions() -> None:
    user = User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=hash_password("correct horse battery staple"),
        auth_version=1,
    )

    class Session:
        def __init__(self) -> None:
            self.executed = []
            self.committed = False

        async def scalar(self, statement: object) -> User:
            return user

        async def execute(self, statement: object) -> None:
            self.executed.append(statement)

        async def commit(self) -> None:
            self.committed = True

    session = Session()
    asyncio.run(change_password(
        PasswordChangeRequest(
            currentPassword="correct horse battery staple",
            newPassword="a different long password",
        ),
        user,
        session,
    ))
    assert session.committed
    assert user.auth_version == 2
    assert verify_password("a different long password", user.password_hash)
    assert "UPDATE refresh_sessions" in str(session.executed[0])


def test_wrong_current_password_does_not_change_credentials() -> None:
    original_hash = hash_password("correct horse battery staple")
    user = User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=original_hash,
        auth_version=1,
    )

    class Session:
        async def scalar(self, statement: object) -> User:
            return user

        async def execute(self, statement: object) -> None:
            raise AssertionError("refresh sessions should not be revoked")

        async def commit(self) -> None:
            raise AssertionError("password change should not commit")

    with pytest.raises(HTTPException) as error:
        asyncio.run(change_password(
            PasswordChangeRequest(
                currentPassword="wrong",
                newPassword="a different long password",
            ),
            user,
            Session(),
        ))
    assert error.value.status_code == 400
    assert error.value.detail == "Current password is incorrect."
    assert user.password_hash == original_hash
    assert user.auth_version == 1


def test_same_password_is_rejected_without_revoking_sessions() -> None:
    original_hash = hash_password("correct horse battery staple")
    user = User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=original_hash,
        auth_version=1,
    )

    class Session:
        async def scalar(self, statement: object) -> User:
            return user

        async def execute(self, statement: object) -> None:
            raise AssertionError("refresh sessions should not be revoked")

        async def commit(self) -> None:
            raise AssertionError("password change should not commit")

    with pytest.raises(HTTPException) as error:
        asyncio.run(change_password(
            PasswordChangeRequest(
                currentPassword="correct horse battery staple",
                newPassword="correct horse battery staple",
            ),
            user,
            Session(),
        ))
    assert error.value.status_code == 400
    assert error.value.detail == "New password must differ from current password."
    assert user.password_hash == original_hash
    assert user.auth_version == 1


def test_old_refresh_after_password_change_cannot_revoke_new_sessions() -> None:
    user = User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=hash_password("a different long password"),
        auth_version=2,
    )
    token = "old-refresh-token"
    record = RefreshSession(
        id=uuid.uuid4(),
        owner_id=user.id,
        token_hash=token_digest(token),
        auth_version=1,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=datetime.now(UTC),
    )

    class Result:
        def scalar_one_or_none(self) -> RefreshSession:
            return record

    class Session:
        async def execute(self, statement: object) -> Result:
            if str(statement).startswith("UPDATE"):
                raise AssertionError("a new session must not be revoked")
            return Result()

        async def scalar(self, statement: object) -> User | RefreshSession:
            return user if "FROM users" in str(statement) else record

        async def commit(self) -> None:
            raise AssertionError("a new session must not be revoked")

    with pytest.raises(HTTPException) as error:
        asyncio.run(rotate_refresh_session(Session(), token))
    assert error.value.status_code == 401
    assert error.value.detail == "Refresh session expired after password change."


def test_refresh_token_digest_is_one_way_and_deterministic() -> None:
    token = "opaque-refresh-token"
    assert token_digest(token) == token_digest(token)
    assert token not in token_digest(token)
    assert len(token_digest(token)) == 64

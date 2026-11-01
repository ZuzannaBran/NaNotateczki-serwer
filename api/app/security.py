import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import database_session
from .models import RefreshSession, User
from .settings import get_settings


password_hash = PasswordHash.recommended()
dummy_password_hash = password_hash.hash(secrets.token_urlsafe(32))
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(user_id: uuid.UUID, auth_version: int) -> tuple[str, int]:
    settings = get_settings()
    seconds = settings.access_token_minutes * 60
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "typ": "access",
            "ver": auth_version,
            "iss": settings.issuer,
            "iat": now,
            "exp": now + timedelta(seconds=seconds),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, seconds


def decode_access_token(token: str) -> uuid.UUID:
    return decode_access_token_claims(token)[0]


def decode_access_token_claims(token: str) -> tuple[uuid.UUID, datetime, int]:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.issuer,
            options={"require": ["sub", "typ", "ver", "iss", "iat", "exp"]},
        )
        if payload["typ"] != "access":
            raise ValueError("wrong token type")
        version = payload["ver"]
        if type(version) is not int or version < 1:
            raise ValueError("invalid session version")
        return (
            uuid.UUID(payload["sub"]),
            datetime.fromtimestamp(payload["exp"], UTC),
            version,
        )
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(database_session),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication is required.")
    user_id, _, auth_version = decode_access_token_claims(credentials.credentials)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="The account no longer exists.")
    if user.auth_version != auth_version:
        raise HTTPException(
            status_code=401, detail="Session has expired. Sign in again."
        )
    return user


async def create_refresh_session(
    session: AsyncSession, user: User
) -> tuple[str, RefreshSession]:
    settings = get_settings()
    token = secrets.token_urlsafe(48)
    record = RefreshSession(
        id=uuid.uuid4(),
        owner_id=user.id,
        token_hash=token_digest(token),
        auth_version=user.auth_version,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    session.add(record)
    return token, record


async def rotate_refresh_session(
    session: AsyncSession, token: str
) -> tuple[User, str]:
    digest = token_digest(token)
    result = await session.execute(
        select(RefreshSession).where(RefreshSession.token_hash == digest)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    user = await session.scalar(
        select(User).where(User.id == record.owner_id).with_for_update()
    )
    if user is None:
        raise HTTPException(status_code=401, detail="The account no longer exists.")
    record = await session.scalar(
        select(RefreshSession)
        .where(RefreshSession.id == record.id)
        .with_for_update()
    )
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    now = datetime.now(UTC)
    if record.revoked_at is not None:
        if record.auth_version != user.auth_version:
            raise HTTPException(
                status_code=401,
                detail="Refresh session expired after password change.",
            )
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.owner_id == record.owner_id)
            .values(revoked_at=now)
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="Refresh token reuse detected.")
    if record.auth_version != user.auth_version:
        raise HTTPException(
            status_code=401,
            detail="Refresh session expired after password change.",
        )
    if record.expires_at <= now:
        record.revoked_at = now
        await session.commit()
        raise HTTPException(status_code=401, detail="Refresh token expired.")
    record.revoked_at = now
    new_token, _ = await create_refresh_session(session, user)
    return user, new_token

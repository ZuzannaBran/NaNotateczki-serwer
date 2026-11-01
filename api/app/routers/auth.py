import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import database_session
from ..models import RefreshSession, User
from ..rate_limit import auth_limiter
from ..realtime_hub import hub
from ..schemas import (
    Credentials,
    PasswordChangeRequest,
    RefreshRequest,
    RegistrationCredentials,
)
from ..security import (
    create_access_token,
    create_refresh_session,
    current_user,
    dummy_password_hash,
    hash_password,
    rotate_refresh_session,
    token_digest,
    verify_password,
)
from ..settings import get_settings


router = APIRouter(prefix="/v1/auth", tags=["auth"])


def session_response(user: User, refresh_token: str) -> dict:
    access_token, expires_in = create_access_token(user.id, user.auth_version)
    return {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresIn": expires_in,
        "userId": str(user.id),
        "username": user.username,
    }


async def limit_auth(request: Request, username: str) -> None:
    client = request.client.host if request.client else "unknown"
    await auth_limiter.check(f"ip:{client}")
    await auth_limiter.check(f"account:{username.lower()}")


@router.post("/register", status_code=201)
async def register(
    credentials: RegistrationCredentials,
    request: Request,
    session: AsyncSession = Depends(database_session),
) -> dict:
    settings = get_settings()
    if not settings.allow_registration:
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    username = credentials.username.lower()
    await limit_auth(request, username)
    user = User(
        id=uuid.uuid4(),
        username=username,
        password_hash=hash_password(credentials.password),
        auth_version=1,
    )
    session.add(user)
    try:
        await session.flush()
        refresh_token, _ = await create_refresh_session(session, user)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Account already exists.") from error
    return session_response(user, refresh_token)


@router.post("/login")
async def login(
    credentials: Credentials,
    request: Request,
    session: AsyncSession = Depends(database_session),
) -> dict:
    username = credentials.username.lower()
    await limit_auth(request, username)
    result = await session.execute(
        select(User).where(User.username == username).with_for_update()
    )
    user = result.scalar_one_or_none()
    encoded_hash = user.password_hash if user is not None else dummy_password_hash
    password_valid = verify_password(credentials.password, encoded_hash)
    if user is None or not password_valid:
        raise HTTPException(status_code=401, detail="Invalid e-mail or password.")
    refresh_token, _ = await create_refresh_session(session, user)
    await session.commit()
    return session_response(user, refresh_token)


@router.post("/refresh")
async def refresh(
    request: RefreshRequest,
    session: AsyncSession = Depends(database_session),
) -> dict:
    user, refresh_token = await rotate_refresh_session(session, request.refreshToken)
    await session.commit()
    return session_response(user, refresh_token)


@router.post("/logout", status_code=204)
async def logout(
    request: RefreshRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> None:
    result = await session.execute(
        select(RefreshSession).where(
            RefreshSession.token_hash == token_digest(request.refreshToken),
            RefreshSession.owner_id == user.id,
        )
    )
    record = result.scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()


@router.post("/change-password", status_code=204)
async def change_password(
    body: PasswordChangeRequest,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(database_session),
) -> None:
    await auth_limiter.check(f"password-change:{user.id}")
    locked_user = await session.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None:
        raise HTTPException(status_code=401, detail="The account no longer exists.")
    if not verify_password(body.currentPassword, locked_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if verify_password(body.newPassword, locked_user.password_hash):
        raise HTTPException(
            status_code=400,
            detail="New password must differ from current password.",
        )
    locked_user.password_hash = hash_password(body.newPassword)
    locked_user.auth_version += 1
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.owner_id == locked_user.id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    await hub.publish(locked_user.id, {"type": "session_revoked"})


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {"userId": str(user.id), "username": user.username}

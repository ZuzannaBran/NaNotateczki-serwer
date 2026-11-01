"""Provision an account from inside the API container without opening registration."""

import asyncio
import getpass
import sys
import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import session_factory
from .models import User
from .schemas import RegistrationCredentials
from .security import hash_password


async def create_user(email: str, password: str) -> None:
    credentials = RegistrationCredentials(email=email, password=password)
    normalized_email = str(credentials.email).lower()
    async with session_factory() as session:
        existing = await session.scalar(select(User.id).where(User.email == normalized_email))
        if existing is not None:
            raise ValueError("Account already exists")
        session.add(
            User(
                id=uuid.uuid4(),
                email=normalized_email,
                password_hash=hash_password(credentials.password),
            )
        )
        try:
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise ValueError("Account already exists") from error


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m app.create_user EMAIL")
    password = getpass.getpass("Password (8 to 128 characters): ")
    confirmation = getpass.getpass("Repeat password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    try:
        asyncio.run(create_user(sys.argv[1], password))
    except (ValidationError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print("Account created")


if __name__ == "__main__":
    main()

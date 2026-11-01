from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .settings import get_settings


settings = get_settings()
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=3,
    max_overflow=2,
)
session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def database_session() -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session

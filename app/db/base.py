"""
SQLAlchemy async engine + session factory.

We use async sessions (asyncpg driver) throughout the application layer.
Alembic migrations use the sync URL (psycopg2) because Alembic's env.py
runs synchronously.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    pool_pre_ping=True,   # detect stale connections automatically
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # avoid lazy-load errors after commit in async context
)


class Base(DeclarativeBase):
    """All ORM models inherit from this."""
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a session and always closes it."""
    async with AsyncSessionLocal() as session:
        yield session

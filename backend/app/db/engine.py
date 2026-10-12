from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    """`postgresql+asyncpg://...`. 연결이 끊긴 채 풀에 남아 있으면 쓰기 전에 확인한다."""
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)

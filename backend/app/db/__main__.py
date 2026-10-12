import argparse
import asyncio
import sys

from app.core.config import Settings
from app.db import migrate
from app.db.engine import make_engine
from app.db.seed import upsert_regions


async def _seed_regions(url: str) -> int:
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            count = await conn.run_sync(upsert_regions)
    finally:
        await engine.dispose()
    print(f"법정동 {count}건을 regions에 반영했습니다")
    return 0


async def _status(url: str) -> int:
    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            current = await conn.run_sync(migrate.current_revision)
    finally:
        await engine.dispose()
    head = migrate.head_revision()
    hint = "" if current == head else "  → `python -m app.db upgrade` 필요"
    print(f"현재 {current or '(없음)'} / 최신 {head}{hint}")
    return 0 if current == head else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description="DB 마이그레이션·시드")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("upgrade", help="최신 스키마로 올린다 (법정동 시드 포함)")
    sub.add_parser("status", help="현재 마이그레이션 버전 확인")
    sub.add_parser("seed-regions", help="법정동 CSV를 regions에 다시 반영 (연 1회 CSV 갱신 후)")
    args = parser.parse_args()

    url = Settings().database_url
    if not url:
        print("PUGURIN_DATABASE_URL이 필요합니다 (예: postgresql+asyncpg://pugurin:pugurin@localhost:5432/pugurin)")
        return 2
    if args.command == "upgrade":
        migrate.upgrade(url)
        return 0
    if args.command == "status":
        return asyncio.run(_status(url))
    return asyncio.run(_seed_regions(url))


if __name__ == "__main__":
    sys.exit(main())

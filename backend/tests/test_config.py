"""DB 주소는 호스팅마다 모양이 달라서(Render는 postgresql://, Heroku식 postgres://) 받는 쪽에서 asyncpg 형식으로 맞춘다."""

import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", ""),
        (
            "postgresql+asyncpg://pugurin:pw@db:5432/pugurin",
            "postgresql+asyncpg://pugurin:pw@db:5432/pugurin",
        ),
        # Render 내부 주소
        ("postgresql://pugurin:pw@dpg-abc-a/pugurin", "postgresql+asyncpg://pugurin:pw@dpg-abc-a/pugurin"),
        ("postgres://u:pw@host:5432/db", "postgresql+asyncpg://u:pw@host:5432/db"),
        # 외부 주소에 붙이는 sslmode는 asyncpg가 모르는 이름이라 ssl로 바꾼다
        (
            "postgresql://u:pw@dpg-abc.singapore-postgres.render.com/db?sslmode=require",
            "postgresql+asyncpg://u:pw@dpg-abc.singapore-postgres.render.com/db?ssl=require",
        ),
    ],
)
def test_database_url_is_normalised_for_asyncpg(given, expected):
    assert Settings(database_url=given).database_url == expected

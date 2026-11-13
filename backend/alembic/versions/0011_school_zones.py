"""school_zones·school_zone_schools: 초등학교 통학구역(한국교육시설안전원 학구도)과 배정 학교

`python -m app.ingestion school-zones`가 학구도 파일로 두 표를 통째로 갈아 끼운다.
학교는 학구도 연계정보의 학교명으로 `schools`(나이스)에 맞춘다.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE school_zones (
            zone_id          text PRIMARY KEY,                   -- 학구ID(예: Z000106316)
            name             text NOT NULL,                      -- 학구명(예: 해빛초통학구역)
            kind             text NOT NULL,                      -- 통학구역 / 공동통학구역
            geom             geometry(MultiPolygon, 4326) NOT NULL,  -- 원천 EPSG:5186을 변환
            base_date        date,                               -- 원천 데이터 기준일
            loaded_at        timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX school_zones_geom ON school_zones USING gist (geom)")
    op.execute(
        """
        CREATE TABLE school_zone_schools (
            zone_id          text NOT NULL REFERENCES school_zones (zone_id) ON DELETE CASCADE,
            school_id        text NOT NULL REFERENCES schools (id),
            PRIMARY KEY (zone_id, school_id)
        )
        """
    )
    op.execute("CREATE INDEX school_zone_schools_school ON school_zone_schools (school_id)")


def downgrade() -> None:
    op.execute("DROP TABLE school_zone_schools, school_zones")

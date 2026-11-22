"""redevelopment_zones: 정비구역 경계(브이월드 용도지역지구정보 SHP)와 우리 정비사업 구역 잇기

- 경계 종류(layer): zone(정비구역) / promotion(재정비촉진구역) / planned(정비예정구역)
- redevelopments.office_geom: 조합 사무실 좌표. 구역 위치가 아니라 경계와 잇는 데에만 쓴다

파일을 받아 `python -m app.ingestion redevelopment-zones`로 통째로 갈아 끼운다. 정비사업 동기화 뒤에 다시 잇는다.
설계: docs/REDEVELOPMENT_ZONES.md

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE redevelopment_zones (
            zone_id           text PRIMARY KEY,                        -- 원천 도형 ID(A1)
            layer             text NOT NULL CHECK (layer IN ('zone', 'promotion', 'planned')),
            name              text,                                    -- 구역 이름. 원천이 비었거나 안내 문구면 NULL
            notice            text,                                    -- 이름 칸에 고시 번호만 있을 때(예: 부산광역시 고시 제2016-415호)
            designated_on     date,                                    -- 고시일(A7)
            sigungu_code      text NOT NULL,                           -- 시군구 코드 5자리(A4)
            geom              geometry(MultiPolygon, 4326) NOT NULL,   -- 원천 EPSG:5186을 변환
            label_point       geometry(Point, 4326) NOT NULL,          -- 경계 안의 대표점(ST_PointOnSurface). 마커 위치 보강용
            area_m2           double precision NOT NULL,
            redevelopment_id  text REFERENCES redevelopments (id) ON DELETE SET NULL,  -- 이어진 우리 구역(조각이면 여럿이 같은 구역)
            link_method       text CHECK (link_method IN ('point', 'name', 'office')),
            base_date         date,                                    -- 원천 기준일(A3)
            loaded_at         timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX redevelopment_zones_geom ON redevelopment_zones USING gist (geom)")
    op.execute("ALTER TABLE redevelopments ADD COLUMN office_geom geometry(Point, 4326)")
    # 한 구역이 조각 여러 개로 나뉘면 조각마다 같은 구역을 가리킨다(일대다)
    op.execute(
        "CREATE INDEX redevelopment_zones_redevelopment ON redevelopment_zones (redevelopment_id) "
        "WHERE redevelopment_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE redevelopment_zones")
    op.execute("ALTER TABLE redevelopments DROP COLUMN office_geom")

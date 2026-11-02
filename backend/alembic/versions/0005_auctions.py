"""auctions: 부산 경매·공매 물건 (경매마당 API, 원천은 대법원 법원경매정보·온비드)

개인정보(당사자·임차인·응찰자 이름, 점유조사 메모, 등기 권리자, 청구액)는 받는 즉시 버리고 저장하지 않는다.
진행 목록에서 빠진 물건은 지우지 않고 `closed_at`을 채운다.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(
        "ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ('collect', 'backfill', 'aggregate', 'auction'))"
    )
    op.execute(
        """
        CREATE TABLE auctions (
            id                text PRIMARY KEY,               -- 경매마당 product_id (CA-..., GO-...)
            source            text NOT NULL CHECK (source IN ('court', 'public')),  -- court=법원 경매, public=공매
            case_no           text NOT NULL,                  -- 사건번호(2024타경104630) / 공매관리번호
            court_name        text,
            title             text,                           -- 공매 물건명
            usage             text,                           -- 물건용도 원문(아파트, 오피스텔(주거), 대지 ...)
            address           text NOT NULL,                  -- 대표 소재지(도로명, 호수 포함)
            jibun_address     text,
            region_code       varchar(10),                    -- 지오코딩으로 얻은 법정동 코드
            pnu               char(19),                       -- 지번까지 맞았을 때만. 단지(complexes.pnu)와 잇는 키
            location          geometry(Point, 4326),
            location_precise  boolean NOT NULL DEFAULT false,
            appraisal_price   bigint,                         -- 감정가(원)
            min_price         bigint,                         -- 최저가·최저입찰가(원)
            deposit           bigint,                         -- 입찰보증금(원)
            fail_count        integer,                        -- 유찰횟수
            sale_date         date,                           -- 경매 매각기일 / 공매 입찰 마감일
            bid_start_at      timestamptz,                    -- 공매만
            bid_end_at        timestamptz,
            land_area_m2      numeric(14, 4),
            building_area_m2  numeric(14, 4),
            is_share          boolean NOT NULL DEFAULT false, -- 지분 매각
            status            text,                           -- 진행 상태 원문
            schedule          jsonb NOT NULL DEFAULT '[]',    -- 기일 이력(날짜·종류·최저가·결과·낙찰가)
            results           jsonb NOT NULL DEFAULT '[]',    -- 매각 결과(응찰자 수·최고가), 응찰자 이름 없음
            list_hash         text NOT NULL,                  -- 목록 값이 바뀌었는지 보고 상세를 다시 받는다
            first_seen_at     timestamptz NOT NULL DEFAULT now(),
            last_seen_at      timestamptz NOT NULL,
            detail_fetched_at timestamptz,
            closed_at         timestamptz,                    -- 진행 목록에서 빠진 시각(매각·취하·변경 등)
            updated_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX auctions_open_location ON auctions USING gist (location) WHERE closed_at IS NULL")
    op.execute("CREATE INDEX auctions_pnu ON auctions (pnu) WHERE pnu IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP TABLE auctions")
    op.execute("DELETE FROM etl_runs WHERE job = 'auction'")
    op.execute("ALTER TABLE etl_runs DROP CONSTRAINT etl_runs_job_check")
    op.execute(
        "ALTER TABLE etl_runs ADD CONSTRAINT etl_runs_job_check CHECK (job IN ('collect', 'backfill', 'aggregate'))"
    )

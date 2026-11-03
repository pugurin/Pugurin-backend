"""academies: 단지 주변 학원·교습소 (나이스 학원교습소정보)

원천 목록에서 빠진 학원은 지우지 않고 `closed_at`을 채운다(휴원·폐원). 동기화는 학교·병원과 같은 'facility' 작업이다.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE academies (
            id               text PRIMARY KEY,           -- 학원지정번호(ACA_ASNUM)
            name             text NOT NULL,
            kind             text,                       -- 학원 / 교습소
            field            text,                       -- 분야: 입시.검정 및 보습 / 예능(대) / 국제화 / 독서실 …
            course           text,                       -- 교습과정: 보습 / 음악 / 미술 …
            address          text NOT NULL,
            region_code      varchar(10),
            location         geometry(Point, 4326),      -- 도로명 주소를 카카오로 지오코딩
            tel              text,
            last_seen_at     timestamptz NOT NULL,
            closed_at        timestamptz,
            updated_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX academies_open_location ON academies USING gist (location) WHERE closed_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE academies")

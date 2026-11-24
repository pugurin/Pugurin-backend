"""user_interests: 관심 목록을 회원 계정에 묶는다(로그인해야 쓴다)

기기 단위(`device_interests`, 0012)는 없앤다. 기기에 쌓인 관심 목록은 계정으로 옮기지 않고 버린다(결정, docs/AUTH.md).
`X-Device-Id`는 클라이언트가 정하는 값이라 계정 데이터의 열쇠로 쓸 수 없다. 이 뒤로는 로그인한 회원만 관심을 넣으므로
버려지는 것은 이 마이그레이션 전에 쌓인 기기 행뿐이다. downgrade는 빈 표만 다시 만든다(지운 행은 돌아오지 않는다).
대상은 지워지거나 닫혀도 행을 남긴다(목록에서 '종료'로 보이거나 빠진다). 그래서 대상에는 외래키를 걸지 않는다.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_interests (
            user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            kind        text NOT NULL CHECK (kind IN ('complex', 'auction')),
            target_id   text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, kind, target_id)
        )
        """
    )
    # 기기 목록은 옮기지 않고 버린다(위 설명)
    op.execute("DROP TABLE device_interests")


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE device_interests (
            device_id   uuid NOT NULL,
            kind        text NOT NULL CHECK (kind IN ('complex', 'auction')),
            target_id   text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (device_id, kind, target_id)
        )
        """
    )
    op.execute("DROP TABLE user_interests")

"""users·user_identities·refresh_tokens·signup_tokens: 소셜 로그인·회원가입 (F-AUTH-01~05)

- 토큰은 SHA-256 해시만 저장한다. refresh token은 갱신할 때마다 새로 만들고 이전 것은 revoked_at을 채운다.
- 탈퇴하면 소셜 연결 행을 지우고 사용자 행은 익명화한다(같은 소셜 계정으로 다시 가입하면 새 사용자).
설계: docs/AUTH.md

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

PROVIDERS = "'naver', 'kakao', 'google'"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE users (
            id                     uuid PRIMARY KEY,
            nickname               text NOT NULL,             -- 탈퇴하면 '탈퇴한 사용자'
            role                   text NOT NULL DEFAULT 'buyer' CHECK (role IN ('buyer', 'admin')),
            provider               text CHECK (provider IN ({PROVIDERS})),  -- 가입한 소셜. 탈퇴하면 NULL
            marketing_push         boolean NOT NULL DEFAULT false,
            marketing_consented_at timestamptz,               -- 광고성 정보 수신동의 일시(정보통신망법)
            settings               jsonb NOT NULL DEFAULT '{{}}',  -- 기기 간 동기화 설정. 바꾼 값만 둔다
            agreed_at              timestamptz NOT NULL,      -- 필수 약관·개인정보·만 14세 이상 동의 일시
            created_at             timestamptz NOT NULL,
            deleted_at             timestamptz
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE user_identities (
            provider         text NOT NULL CHECK (provider IN ({PROVIDERS})),
            provider_user_id text NOT NULL,
            user_id          uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            created_at       timestamptz NOT NULL,
            PRIMARY KEY (provider, provider_user_id)
        )
        """
    )
    op.execute("CREATE INDEX user_identities_user ON user_identities (user_id)")
    op.execute(
        """
        CREATE TABLE refresh_tokens (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            family_id   uuid NOT NULL,                       -- 처음 로그인한 토큰부터 갱신으로 이어진 묶음
            token_hash  text NOT NULL UNIQUE,                -- SHA-256 hex
            device_id   uuid,                                -- X-Device-Id
            created_at  timestamptz NOT NULL,
            expires_at  timestamptz NOT NULL,
            revoked_at  timestamptz
        )
        """
    )
    op.execute("CREATE INDEX refresh_tokens_user_active ON refresh_tokens (user_id) WHERE revoked_at IS NULL")
    op.execute(
        f"""
        CREATE TABLE signup_tokens (
            token_hash       text PRIMARY KEY,               -- SHA-256 hex. 쓰면 지운다
            provider         text NOT NULL CHECK (provider IN ({PROVIDERS})),
            provider_user_id text NOT NULL,
            profile_nickname text,
            expires_at       timestamptz NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX signup_tokens_expires ON signup_tokens (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE signup_tokens")
    op.execute("DROP TABLE refresh_tokens")
    op.execute("DROP TABLE user_identities")
    op.execute("DROP TABLE users")

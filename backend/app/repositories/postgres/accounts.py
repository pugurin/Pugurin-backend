"""계정 저장소(마이그레이션 0011). 토큰은 SHA-256 해시만 다룬다. 설계: docs/AUTH.md"""

import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.repositories.types import Account, RotateOutcome, SignupClaim, SocialProvider

WITHDRAWN_NICKNAME = "탈퇴한 사용자"

_COLS = "id, nickname, role, provider, marketing_push, marketing_consented_at, settings, created_at"
_USER_COLS = ", ".join(f"u.{c.strip()}" for c in _COLS.split(","))


class _AlreadyLinked(Exception):
    """같은 소셜 계정이 먼저 가입했다. 방금 넣은 사용자 행을 되돌리려고 던진다."""


def _account(row) -> Account:
    return Account(
        id=row["id"],
        nickname=row["nickname"],
        role=row["role"],
        provider=SocialProvider(row["provider"]),
        marketing_push=row["marketing_push"],
        marketing_consented_at=row["marketing_consented_at"],
        settings=dict(row["settings"] or {}),
        created_at=row["created_at"],
    )


class PostgresAccountRepository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def _one(self, conn: AsyncConnection, sql: str, **params):
        return (await conn.execute(text(sql), params)).mappings().first()

    async def find_by_identity(self, provider: SocialProvider, provider_user_id: str) -> Account | None:
        async with self._engine.connect() as conn:
            row = await self._one(
                conn,
                f"""
                SELECT {_USER_COLS}
                FROM user_identities i JOIN users u ON u.id = i.user_id
                WHERE i.provider = :provider AND i.provider_user_id = :puid AND u.deleted_at IS NULL
                """,
                provider=provider.value,
                puid=provider_user_id,
            )
        return _account(row) if row else None

    async def get(self, user_id: UUID) -> Account | None:
        async with self._engine.connect() as conn:
            row = await self._one(conn, f"SELECT {_COLS} FROM users WHERE id = :id AND deleted_at IS NULL", id=user_id)
        return _account(row) if row else None

    async def create(self, claim: SignupClaim, nickname: str, *, marketing: bool, now: datetime) -> Account:
        try:
            async with self._engine.begin() as conn:
                row = await self._one(
                    conn,
                    f"""
                    INSERT INTO users (id, nickname, provider, marketing_push, marketing_consented_at, agreed_at,
                                       created_at)
                    VALUES (:id, :nickname, :provider, :marketing, :consented_at, :now, :now)
                    RETURNING {_COLS}
                    """,
                    id=uuid4(),
                    nickname=nickname,
                    provider=claim.provider.value,
                    marketing=marketing,
                    consented_at=now if marketing else None,
                    now=now,
                )
                # 동시에 같은 소셜 계정이 가입하면 먼저 커밋한 쪽을 기다렸다가 아무것도 넣지 않는다
                linked = await self._one(
                    conn,
                    """
                    INSERT INTO user_identities (provider, provider_user_id, user_id, created_at)
                    VALUES (:provider, :puid, :user_id, :now)
                    ON CONFLICT (provider, provider_user_id) DO NOTHING
                    RETURNING user_id
                    """,
                    provider=claim.provider.value,
                    puid=claim.provider_user_id,
                    user_id=row["id"],
                    now=now,
                )
                if linked is None:
                    raise _AlreadyLinked
                return _account(row)
        except _AlreadyLinked:
            existing = await self.find_by_identity(claim.provider, claim.provider_user_id)
            if existing is None:  # 그 사이 탈퇴까지 했다면 다시 만든다
                return await self.create(claim, nickname, marketing=marketing, now=now)
            return existing

    async def update(self, user_id: UUID, *, nickname: str | None, settings: dict[str, bool] | None) -> Account | None:
        async with self._engine.begin() as conn:
            row = await self._one(
                conn,
                f"""
                UPDATE users SET nickname = COALESCE(:nickname, nickname),
                                 settings = settings || CAST(:settings AS jsonb)
                WHERE id = :id AND deleted_at IS NULL
                RETURNING {_COLS}
                """,
                id=user_id,
                nickname=nickname,
                settings=json.dumps(settings or {}),
            )
        return _account(row) if row else None

    async def withdraw(self, user_id: UUID, *, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DELETE FROM signup_tokens s USING user_identities i
                    WHERE i.user_id = :id AND s.provider = i.provider AND s.provider_user_id = i.provider_user_id
                    """
                ),
                {"id": user_id},
            )
            await conn.execute(text("DELETE FROM user_identities WHERE user_id = :id"), {"id": user_id})
            await conn.execute(
                text(
                    """
                    UPDATE users SET nickname = :withdrawn, provider = NULL, marketing_push = false,
                                     marketing_consented_at = NULL, settings = '{}', deleted_at = :now
                    WHERE id = :id AND deleted_at IS NULL
                    """
                ),
                {"id": user_id, "now": now, "withdrawn": WITHDRAWN_NICKNAME},
            )
            await self._revoke_all(conn, user_id, now)

    async def save_signup_token(
        self, token_hash: str, claim: SignupClaim, *, expires_at: datetime, now: datetime
    ) -> None:
        async with self._engine.begin() as conn:
            # 쓰지 않고 만료된 가입 토큰(소셜 식별자)은 여기서 함께 지운다
            await conn.execute(text("DELETE FROM signup_tokens WHERE expires_at <= :now"), {"now": now})
            await conn.execute(
                text(
                    """
                    INSERT INTO signup_tokens (token_hash, provider, provider_user_id, profile_nickname, expires_at)
                    VALUES (:hash, :provider, :puid, :nickname, :expires_at)
                    """
                ),
                {
                    "hash": token_hash,
                    "provider": claim.provider.value,
                    "puid": claim.provider_user_id,
                    "nickname": claim.profile_nickname,
                    "expires_at": expires_at,
                },
            )

    async def take_signup_token(self, token_hash: str, *, now: datetime) -> SignupClaim | None:
        async with self._engine.begin() as conn:
            row = await self._one(
                conn,
                """
                DELETE FROM signup_tokens WHERE token_hash = :hash
                RETURNING provider, provider_user_id, profile_nickname, expires_at
                """,
                hash=token_hash,
            )
        if row is None or row["expires_at"] <= now:
            return None
        return SignupClaim(SocialProvider(row["provider"]), row["provider_user_id"], row["profile_nickname"])

    async def add_refresh_token(
        self,
        user_id: UUID,
        family_id: UUID,
        token_hash: str,
        device_id: UUID | None,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await self._insert_refresh(conn, user_id, family_id, token_hash, device_id, now, expires_at)

    async def rotate_refresh_token(
        self, old_hash: str, new_hash: str, device_id: UUID | None, *, now: datetime, expires_at: datetime
    ) -> tuple[RotateOutcome, UUID | None]:
        async with self._engine.begin() as conn:
            # 같은 토큰으로 동시에 갱신하면 행 잠금으로 한쪽만 rotated, 다른 쪽은 reused가 된다
            row = await self._one(
                conn,
                """
                SELECT id, user_id, family_id, expires_at, revoked_at FROM refresh_tokens
                WHERE token_hash = :hash FOR UPDATE
                """,
                hash=old_hash,
            )
            if row is None:
                return RotateOutcome.unknown, None
            if row["revoked_at"] is not None:
                await self._revoke_all(conn, row["user_id"], now)
                return RotateOutcome.reused, row["user_id"]
            if row["expires_at"] <= now:
                return RotateOutcome.unknown, None
            await conn.execute(
                text("UPDATE refresh_tokens SET revoked_at = :now WHERE id = :id"), {"now": now, "id": row["id"]}
            )
            await self._insert_refresh(conn, row["user_id"], row["family_id"], new_hash, device_id, now, expires_at)
            return RotateOutcome.rotated, row["user_id"]

    async def revoke_refresh_token(self, token_hash: str, *, now: datetime) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("UPDATE refresh_tokens SET revoked_at = :now WHERE token_hash = :hash AND revoked_at IS NULL"),
                {"now": now, "hash": token_hash},
            )

    @staticmethod
    async def _insert_refresh(conn, user_id, family_id, token_hash, device_id, now, expires_at) -> None:
        await conn.execute(
            text(
                """
                INSERT INTO refresh_tokens (user_id, family_id, token_hash, device_id, created_at, expires_at)
                VALUES (:user_id, :family_id, :hash, :device_id, :now, :expires_at)
                """
            ),
            {
                "user_id": user_id,
                "family_id": family_id,
                "hash": token_hash,
                "device_id": device_id,
                "now": now,
                "expires_at": expires_at,
            },
        )

    @staticmethod
    async def _revoke_all(conn: AsyncConnection, user_id: UUID, now: datetime) -> None:
        await conn.execute(
            text("UPDATE refresh_tokens SET revoked_at = :now WHERE user_id = :id AND revoked_at IS NULL"),
            {"now": now, "id": user_id},
        )

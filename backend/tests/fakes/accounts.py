"""테스트용 메모리 계정 저장소. test_accounts_repo.py가 실제 PostGIS 저장소와 같은 테스트로 동작을 맞춘다."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.repositories.postgres.accounts import WITHDRAWN_NICKNAME
from app.repositories.types import Account, RotateOutcome, SignupClaim, SocialProvider


@dataclass
class _Refresh:
    user_id: UUID
    family_id: UUID
    token_hash: str
    device_id: UUID | None
    expires_at: datetime
    revoked_at: datetime | None = None
    replaced_by: "_Refresh | None" = None


class InMemoryAccountRepository:
    def __init__(self):
        self.users: dict[UUID, Account] = {}
        self.deleted: dict[UUID, datetime] = {}
        self.identities: dict[tuple[SocialProvider, str], UUID] = {}
        self.refresh: list[_Refresh] = []
        self.signup: dict[str, tuple[SignupClaim, datetime]] = {}

    async def find_by_identity(self, provider: SocialProvider, provider_user_id: str) -> Account | None:
        user_id = self.identities.get((provider, provider_user_id))
        return await self.get(user_id) if user_id else None

    async def get(self, user_id: UUID) -> Account | None:
        return None if user_id in self.deleted else self.users.get(user_id)

    async def create(self, claim: SignupClaim, nickname: str, *, marketing: bool, now: datetime) -> Account:
        existing = await self.find_by_identity(claim.provider, claim.provider_user_id)
        if existing:
            return existing
        user = Account(uuid4(), nickname, "buyer", claim.provider, marketing, now if marketing else None, {}, now)
        self.users[user.id] = user
        self.identities[(claim.provider, claim.provider_user_id)] = user.id
        return user

    async def update(self, user_id: UUID, *, nickname: str | None, settings: dict[str, bool] | None) -> Account | None:
        user = await self.get(user_id)
        if user is None:
            return None
        user = replace(user, nickname=nickname or user.nickname, settings={**user.settings, **(settings or {})})
        self.users[user_id] = user
        return user

    async def withdraw(self, user_id: UUID, *, now: datetime) -> list[tuple[SocialProvider, str]]:
        if await self.get(user_id) is None:
            return []
        keys = [k for k, v in self.identities.items() if v == user_id]
        for key in keys:
            del self.identities[key]
        self.signup = {h: v for h, v in self.signup.items() if (v[0].provider, v[0].provider_user_id) not in keys}
        self.users[user_id] = replace(
            self.users[user_id],
            nickname=WITHDRAWN_NICKNAME,
            marketing_push=False,
            marketing_consented_at=None,
            settings={},
        )
        self.deleted[user_id] = now
        self.refresh = [r for r in self.refresh if r.user_id != user_id]
        return keys

    async def save_signup_token(
        self, token_hash: str, claim: SignupClaim, *, expires_at: datetime, now: datetime
    ) -> None:
        self.signup = {h: v for h, v in self.signup.items() if v[1] > now}
        self.signup[token_hash] = (claim, expires_at)

    async def take_signup_token(self, token_hash: str, *, now: datetime) -> SignupClaim | None:
        found = self.signup.pop(token_hash, None)
        if found is None or found[1] <= now:
            return None
        return found[0]

    async def add_refresh_token(self, user_id, family_id, token_hash, device_id, *, now, expires_at) -> None:
        self.refresh.append(_Refresh(user_id, family_id, token_hash, device_id, expires_at))

    async def rotate_refresh_token(self, old_hash, new_hash, device_id, *, now, expires_at, grace=timedelta(0)):
        row = next((r for r in self.refresh if r.token_hash == old_hash), None)
        if row is None:
            return RotateOutcome.unknown, None
        if row.revoked_at is not None:
            successor = row.replaced_by
            if successor is not None and successor.revoked_at is None and now - row.revoked_at < grace:
                return RotateOutcome.concurrent, row.user_id
            self._revoke_all(row.user_id, now)
            return RotateOutcome.reused, row.user_id
        if row.expires_at <= now:
            return RotateOutcome.unknown, None
        new = _Refresh(row.user_id, row.family_id, new_hash, device_id, expires_at)
        row.revoked_at, row.replaced_by = now, new
        self.refresh.append(new)
        return RotateOutcome.rotated, row.user_id

    async def revoke_refresh_token(self, token_hash: str, *, now: datetime) -> None:
        for r in self.refresh:
            if r.token_hash == token_hash and r.revoked_at is None:
                r.revoked_at = now

    def _revoke_all(self, user_id: UUID, now: datetime) -> None:
        for r in self.refresh:
            if r.user_id == user_id and r.revoked_at is None:
                r.revoked_at = now

    # ── 테스트에서 들여다보기 ──

    async def refresh_families(self, user_id) -> dict:
        families: dict = {}
        for r in self.refresh:
            if r.user_id == user_id:
                families.setdefault(r.family_id, []).append(r.token_hash)
        return families

    async def active_refresh_hashes(self, user_id) -> set[str]:
        return {r.token_hash for r in self.refresh if r.user_id == user_id and r.revoked_at is None}

    async def raw_user(self, user_id) -> dict:
        u = self.users[user_id]
        deleted = user_id in self.deleted
        return {
            "nickname": u.nickname,
            "provider": None if deleted else u.provider.value,
            "marketing_push": u.marketing_push,
            "marketing_consented_at": u.marketing_consented_at,
            "settings": u.settings,
            "deleted_at": self.deleted.get(user_id),
        }

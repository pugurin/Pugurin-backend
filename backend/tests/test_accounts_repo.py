"""계정 저장소. 같은 테스트를 테스트용 메모리 저장소와 실제 PostGIS(Docker 없으면 건너뜀)에 똑같이 돌려
API 테스트가 쓰는 가짜가 실제 저장소와 같게 움직이는지도 확인한다."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db import migrate
from app.db.engine import make_engine
from app.repositories.postgres.accounts import PostgresAccountRepository
from app.repositories.types import RotateOutcome, SignupClaim, SocialProvider
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.accounts import InMemoryAccountRepository

pytestmark = pytest.mark.anyio

NOW = FIXED_NOW
KAKAO = SignupClaim(SocialProvider.kakao, "4012345678", "부산갈매기")


@pytest.fixture(scope="module")
def accounts_db(postgis: str) -> str:
    url = create_database(postgis, "accounts")
    migrate.upgrade(url)
    return url


class InspectablePostgres(PostgresAccountRepository):
    """테스트에서만 쓰는 들여다보기 메서드(메모리 저장소에도 같은 이름이 있다)."""

    async def _rows(self, sql: str, **params):
        async with self._engine.connect() as conn:
            return (await conn.execute(text(sql), params)).mappings().all()

    async def refresh_families(self, user_id) -> dict:
        families: dict = {}
        for r in await self._rows(
            "SELECT family_id, token_hash FROM refresh_tokens WHERE user_id = :u ORDER BY id", u=user_id
        ):
            families.setdefault(r["family_id"], []).append(r["token_hash"])
        return families

    async def active_refresh_hashes(self, user_id) -> set[str]:
        rows = await self._rows(
            "SELECT token_hash FROM refresh_tokens WHERE user_id = :u AND revoked_at IS NULL", u=user_id
        )
        return {r["token_hash"] for r in rows}

    async def raw_user(self, user_id) -> dict:
        [row] = await self._rows(
            "SELECT nickname, provider, marketing_push, marketing_consented_at, settings, deleted_at FROM users WHERE id = :u",
            u=user_id,
        )
        return dict(row)


@pytest.fixture(params=["memory", "postgres"])
def db_url(request) -> str | None:
    # DB 준비(asyncio.run)는 이벤트 루프 밖인 동기 픽스처에서 한다
    return None if request.param == "memory" else request.getfixturevalue("accounts_db")


@pytest.fixture
async def repo(db_url):
    if db_url is None:
        yield InMemoryAccountRepository()
        return
    engine = make_engine(db_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, user_identities, refresh_tokens, signup_tokens CASCADE"))
    yield InspectablePostgres(engine)
    await engine.dispose()


async def test_create_then_find_by_identity(repo):
    assert await repo.find_by_identity(SocialProvider.kakao, "4012345678") is None
    user = await repo.create(KAKAO, "부산사람", marketing=True, now=NOW)
    assert (user.nickname, user.role, user.provider, user.marketing_push) == ("부산사람", "buyer", "kakao", True)
    assert user.marketing_consented_at == NOW and user.created_at == NOW and user.settings == {}
    assert await repo.find_by_identity(SocialProvider.kakao, "4012345678") == user
    assert await repo.get(user.id) == user
    # 다른 제공자의 같은 ID는 다른 사람이다
    assert await repo.find_by_identity(SocialProvider.naver, "4012345678") is None


async def test_without_marketing_consent_there_is_no_consent_time(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    assert (user.marketing_push, user.marketing_consented_at) == (False, None)


async def test_creating_the_same_social_account_twice_returns_the_first_user(repo):
    first = await repo.create(KAKAO, "처음", marketing=False, now=NOW)
    again = await repo.create(KAKAO, "두번째", marketing=True, now=NOW + timedelta(minutes=1))
    assert again == first


async def test_update_nickname_and_merge_settings(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    updated = await repo.update(user.id, nickname="해운대", settings={"loading_tips": False})
    assert (updated.nickname, updated.settings) == ("해운대", {"loading_tips": False})
    updated = await repo.update(user.id, nickname=None, settings={"unseen_term_dots": True})
    assert (updated.nickname, updated.settings) == ("해운대", {"loading_tips": False, "unseen_term_dots": True})
    assert await repo.update(uuid4(), nickname="x", settings=None) is None


async def test_signup_tokens_are_single_use_and_expire(repo):
    await repo.save_signup_token("h1", KAKAO, expires_at=NOW + timedelta(minutes=10), now=NOW)
    assert await repo.take_signup_token("h1", now=NOW) == KAKAO
    assert await repo.take_signup_token("h1", now=NOW) is None
    await repo.save_signup_token("h2", KAKAO, expires_at=NOW + timedelta(minutes=10), now=NOW)
    assert await repo.take_signup_token("h2", now=NOW + timedelta(minutes=10)) is None
    assert await repo.take_signup_token("nope", now=NOW) is None


async def test_refresh_rotation_keeps_the_family_and_revokes_the_old_token(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    family = uuid4()
    device = uuid4()
    await repo.add_refresh_token(user.id, family, "r1", device, now=NOW, expires_at=NOW + timedelta(days=14))
    later = NOW + timedelta(days=1)
    assert await repo.rotate_refresh_token("r1", "r2", device, now=later, expires_at=later + timedelta(days=14)) == (
        RotateOutcome.rotated,
        user.id,
    )
    assert await repo.refresh_families(user.id) == {family: ["r1", "r2"]}
    assert await repo.active_refresh_hashes(user.id) == {"r2"}


async def test_reusing_a_revoked_refresh_token_revokes_every_session_of_that_user(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    exp = NOW + timedelta(days=14)
    await repo.add_refresh_token(user.id, uuid4(), "phone", None, now=NOW, expires_at=exp)
    await repo.add_refresh_token(user.id, uuid4(), "web", None, now=NOW, expires_at=exp)
    await repo.rotate_refresh_token("phone", "phone2", None, now=NOW, expires_at=exp)
    assert await repo.rotate_refresh_token("phone", "phone3", None, now=NOW, expires_at=exp) == (
        RotateOutcome.reused,
        user.id,
    )
    assert await repo.active_refresh_hashes(user.id) == set()


async def test_unknown_or_expired_refresh_tokens_are_not_rotated(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    await repo.add_refresh_token(user.id, uuid4(), "r1", None, now=NOW, expires_at=NOW + timedelta(days=14))
    assert await repo.rotate_refresh_token("nope", "x", None, now=NOW, expires_at=NOW) == (RotateOutcome.unknown, None)
    expired_at = NOW + timedelta(days=14)
    assert await repo.rotate_refresh_token("r1", "r2", None, now=expired_at, expires_at=expired_at) == (
        RotateOutcome.unknown,
        None,
    )
    assert await repo.active_refresh_hashes(user.id) == {"r1"}


async def test_revoke_a_single_refresh_token(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    exp = NOW + timedelta(days=14)
    await repo.add_refresh_token(user.id, uuid4(), "a", None, now=NOW, expires_at=exp)
    await repo.add_refresh_token(user.id, uuid4(), "b", None, now=NOW, expires_at=exp)
    await repo.revoke_refresh_token("a", now=NOW)
    await repo.revoke_refresh_token("unknown", now=NOW)
    assert await repo.active_refresh_hashes(user.id) == {"b"}
    # 로그아웃한 토큰을 다시 쓰면 재사용으로 본다
    assert (await repo.rotate_refresh_token("a", "c", None, now=NOW, expires_at=exp))[0] == RotateOutcome.reused


GRACE = timedelta(seconds=30)


async def rotated_pair(repo):
    """r1 → r2로 한 번 갱신한 사용자."""
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    exp = NOW + timedelta(days=14)
    await repo.add_refresh_token(user.id, uuid4(), "r1", None, now=NOW, expires_at=exp)
    await repo.add_refresh_token(user.id, uuid4(), "other-device", None, now=NOW, expires_at=exp)
    assert (await repo.rotate_refresh_token("r1", "r2", None, now=NOW, expires_at=exp, grace=GRACE))[0] == (
        RotateOutcome.rotated
    )
    return user, exp


async def test_a_just_rotated_token_coming_again_within_the_grace_is_only_refused(repo):
    user, exp = await rotated_pair(repo)
    later = NOW + timedelta(seconds=29)
    assert await repo.rotate_refresh_token("r1", "r3", None, now=later, expires_at=exp, grace=GRACE) == (
        RotateOutcome.concurrent,
        user.id,
    )
    assert await repo.active_refresh_hashes(user.id) == {"r2", "other-device"}


async def test_reuse_after_the_grace_revokes_everything(repo):
    user, exp = await rotated_pair(repo)
    later = NOW + timedelta(seconds=30)
    assert (await repo.rotate_refresh_token("r1", "r3", None, now=later, expires_at=exp, grace=GRACE))[0] == (
        RotateOutcome.reused
    )
    assert await repo.active_refresh_hashes(user.id) == set()


async def test_reuse_within_the_grace_after_the_successor_was_used_revokes_everything(repo):
    user, exp = await rotated_pair(repo)
    await repo.rotate_refresh_token("r2", "r3", None, now=NOW, expires_at=exp, grace=GRACE)
    assert (await repo.rotate_refresh_token("r1", "r4", None, now=NOW, expires_at=exp, grace=GRACE))[0] == (
        RotateOutcome.reused
    )
    assert await repo.active_refresh_hashes(user.id) == set()


async def test_a_logged_out_token_has_no_grace(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    exp = NOW + timedelta(days=14)
    await repo.add_refresh_token(user.id, uuid4(), "r1", None, now=NOW, expires_at=exp)
    await repo.add_refresh_token(user.id, uuid4(), "b", None, now=NOW, expires_at=exp)
    await repo.revoke_refresh_token("r1", now=NOW)
    assert (await repo.rotate_refresh_token("r1", "r2", None, now=NOW, expires_at=exp, grace=GRACE))[0] == (
        RotateOutcome.reused
    )
    assert await repo.active_refresh_hashes(user.id) == set()


async def test_withdraw_anonymizes_unlinks_and_revokes(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=True, now=NOW)
    await repo.update(user.id, nickname=None, settings={"loading_tips": False})
    await repo.add_refresh_token(user.id, uuid4(), "r1", None, now=NOW, expires_at=NOW + timedelta(days=14))
    unlinked = await repo.withdraw(user.id, now=NOW + timedelta(days=1))
    # 지운 소셜 연결을 돌려준다(탈퇴 뒤 제공자 연결 끊기에 쓴다)
    assert unlinked == [(SocialProvider.kakao, "4012345678")]
    # 탈퇴하면 refresh token 행은 폐기가 아니라 삭제한다
    assert await repo.refresh_families(user.id) == {}
    assert await repo.get(user.id) is None
    assert await repo.find_by_identity(SocialProvider.kakao, "4012345678") is None
    assert await repo.active_refresh_hashes(user.id) == set()
    assert await repo.raw_user(user.id) == {
        "nickname": "탈퇴한 사용자",
        "provider": None,
        "marketing_push": False,
        "marketing_consented_at": None,
        "settings": {},
        "deleted_at": NOW + timedelta(days=1),
    }
    # 같은 소셜 계정으로 다시 가입하면 새 사용자다
    again = await repo.create(KAKAO, "다시왔어요", marketing=False, now=NOW + timedelta(days=2))
    assert again.id != user.id
    assert await repo.update(user.id, nickname="부활", settings=None) is None


async def test_withdrawing_twice_unlinks_nothing_the_second_time(repo):
    user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
    await repo.withdraw(user.id, now=NOW)
    assert await repo.withdraw(user.id, now=NOW) == []


async def test_two_refreshes_with_the_same_token_at_the_same_time_on_postgres(accounts_db):
    """동시에 같은 토큰으로 갱신하면 하나는 성공, 하나는 거절되고 새 토큰은 살아 있다(행 잠금 + 유예)."""
    import asyncio

    engine = make_engine(accounts_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE users, user_identities, refresh_tokens, signup_tokens CASCADE"))
        repo = InspectablePostgres(engine)
        user = await repo.create(KAKAO, "부산사람", marketing=False, now=NOW)
        exp = NOW + timedelta(days=14)
        await repo.add_refresh_token(user.id, uuid4(), "r1", None, now=NOW, expires_at=exp)
        results = await asyncio.gather(
            repo.rotate_refresh_token("r1", "a", None, now=NOW, expires_at=exp, grace=GRACE),
            repo.rotate_refresh_token("r1", "b", None, now=NOW, expires_at=exp, grace=GRACE),
        )
        outcomes = sorted(r[0].value for r in results)
        assert outcomes == ["concurrent", "rotated"]
        winner = "a" if results[0][0] is RotateOutcome.rotated else "b"
        assert await repo.active_refresh_hashes(user.id) == {winner}
    finally:
        await engine.dispose()

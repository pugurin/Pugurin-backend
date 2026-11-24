"""로그인·가입·토큰 갱신·로그아웃·내 정보·탈퇴 API (docs/AUTH.md). 저장은 테스트용 메모리 저장소, 제공자는 가짜."""

import logging
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dates import latest_etl_time
from app.db import migrate
from app.main import create_app
from app.repositories.memory.dataset import MarketDataset
from app.repositories.memory.market import InMemoryMarketRepository
from app.repositories.types import SocialProvider
from tests.conftest import API, FIXED_NOW, create_database
from tests.fakes.accounts import InMemoryAccountRepository
from tests.fakes.oauth import FakeOAuth, FakeUnlinker

SECRET = "test-secret-" + "x" * 32
CALLBACK = "https://pugurin.netlify.app/auth/callback"
WEB = {"X-Client": "web"}
AGREE = {"terms": True, "privacy": True, "age14": True, "marketing": False}


def empty_market() -> InMemoryMarketRepository:
    """인증 테스트는 시장 데이터가 필요 없어 가짜 시장(만드는 데 1초 가까이 걸린다)을 만들지 않는다."""
    return InMemoryMarketRepository(latest_etl_time(FIXED_NOW), MarketDataset((), ()), {})


class MutableClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class World:
    def __init__(self, **settings):
        self.clock = MutableClock(FIXED_NOW)
        # database_url을 주면 실제 PostGIS 저장소(앱이 직접 조립), 아니면 테스트용 메모리 저장소
        self.repo = None if settings.get("database_url") else InMemoryAccountRepository()
        self.providers = {p: FakeOAuth(p) for p in SocialProvider}
        self.unlinker = FakeUnlinker(repo=self.repo)
        options = {"jwt_secret": SECRET, "auth_redirect_uris": [CALLBACK], **settings}
        app = create_app(
            settings=Settings(**options),
            clock=self.clock,
            account_repo=self.repo,
            oauth_providers=self.providers,
            social_unlinkers={SocialProvider.kakao: self.unlinker},
            market_repo=empty_market(),
        )
        self.client = TestClient(app, headers={"X-Device-Id": str(uuid4())}, base_url="https://testserver")

    def __enter__(self) -> "World":
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    @property
    def kakao(self) -> FakeOAuth:
        return self.providers[SocialProvider.kakao]

    def app_login(self, provider="kakao", token="sdk-1", **headers):
        return self.client.post(f"{API}/auth/oauth/{provider}", json={"access_token": token}, headers=headers)

    def signup(self, signup_token, nickname="부산사람", agreements=None, **headers):
        body = {"signup_token": signup_token, "nickname": nickname, "agreements": agreements or AGREE}
        return self.client.post(f"{API}/auth/signup", json=body, headers=headers)

    def new_member(self, token="sdk-1", user_id="k-1", nickname="부산사람", **agreements) -> dict:
        """앱으로 처음 로그인해 가입까지 마친 세션."""
        self.kakao.tokens[token] = self.kakao.user(user_id, "카카오이름")
        first = self.app_login(token=token).json()["data"]
        r = self.signup(first["signup_token"], nickname, {**AGREE, **agreements})
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def me(self, access_token: str):
        return self.client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {access_token}"})

    def refresh(self, refresh_token: str):
        return self.client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token})


@pytest.fixture
def world() -> World:
    with World() as w:
        yield w


@pytest.fixture(scope="module")
def auth_db(postgis: str) -> str:
    url = create_database(postgis, "auth_api")
    migrate.upgrade(url)
    return url


def test_the_whole_flow_against_postgres(auth_db):
    """가입 → 갱신 → 재사용 감지 → 웹 로그인 → 탈퇴 → 재가입을 실제 DB로 한 번 돌린다."""
    with World(database_url=auth_db) as w:
        session = w.new_member(user_id="pg-1", marketing=True)
        assert w.me(session["access_token"]).json()["data"]["consents"]["marketing_push"] is True
        rotated = w.refresh(session["refresh_token"]).json()["data"]
        w.clock.advance(seconds=31)
        assert w.refresh(session["refresh_token"]).status_code == 401
        assert w.refresh(rotated["refresh_token"]).status_code == 401
        w.kakao.codes["code-1"] = w.kakao.user("pg-1")
        r = web_login(w)
        assert r.json()["data"]["user"]["id"] == session["user"]["id"]
        access = r.json()["data"]["access_token"]
        assert w.client.post(f"{API}/auth/refresh", headers=WEB).status_code == 200
        patched = w.client.patch(
            f"{API}/users/me", json={"nickname": "광안리"}, headers={"Authorization": f"Bearer {access}"}
        )
        assert patched.json()["data"]["nickname"] == "광안리"
        assert w.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {access}"}).status_code == 204
        assert w.me(access).status_code == 401
        assert w.app_login().json()["data"]["is_new_user"] is True


def error(r) -> tuple[int, str, str | None]:
    e = r.json()["error"]
    return r.status_code, e["code"], e["field"]


def set_cookie(r) -> str:
    [cookie] = r.headers.get_list("set-cookie")
    return cookie


# ── 1. 로그인(앱) → 2. 가입 마무리 ──


def test_first_app_login_gives_a_signup_token_instead_of_a_session(world):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1", "카카오이름")
    r = world.app_login()
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_new_user"] is True and data["profile"] == {"nickname": "카카오이름"}
    assert set(data) == {"is_new_user", "signup_token", "profile"} and len(data["signup_token"]) >= 43
    assert r.headers["Cache-Control"] == "no-store"
    # 아직 사용자를 만들지 않았다
    assert world.repo.users == {}


def test_signup_creates_the_user_and_returns_a_session(world):
    session = world.new_member(marketing=True)
    assert session["is_new_user"] is False
    assert set(session) == {"is_new_user", "access_token", "refresh_token", "user"}
    user = session["user"]
    assert user == {
        "id": user["id"],
        "nickname": "부산사람",
        "role": "buyer",
        "provider": "kakao",
        "verified_ownership_count": 0,
        "active_listing_count": 0,
        "consents": {
            "chat_push": True,
            "interest_alert_push": True,
            "study_push": False,
            "study_push_time": "20:00",
            "marketing_push": True,
            "marketing_night_push": False,
            "marketing_consented_at": "2026-10-06T12:00:00+09:00",
            "marketing_reconfirm_due": "2028-10-06T12:00:00+09:00",
        },
        "settings": {"loading_tips": True, "unseen_term_dots": True, "weekly_report": True},
        "created_at": "2026-10-06T12:00:00+09:00",
    }
    r = world.me(session["access_token"])
    assert r.status_code == 200 and r.json()["data"] == user
    assert r.headers["Cache-Control"] == "no-store"


def test_without_marketing_consent_the_consent_fields_are_empty(world):
    consents = world.new_member()["user"]["consents"]
    assert (consents["marketing_push"], consents["marketing_consented_at"], consents["marketing_reconfirm_due"]) == (
        False,
        None,
        None,
    )


def test_a_member_logs_in_directly(world):
    first = world.new_member()
    r = world.app_login()
    data = r.json()["data"]
    assert data["is_new_user"] is False and data["user"]["id"] == first["user"]["id"]
    assert data["refresh_token"] != first["refresh_token"]


@pytest.mark.parametrize("missing", ["terms", "privacy", "age14"])
def test_every_required_agreement_is_checked_and_the_signup_token_survives(world, missing):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    token = world.app_login().json()["data"]["signup_token"]
    r = world.signup(token, agreements={**AGREE, missing: False})
    assert error(r) == (400, "VALIDATION_ERROR", missing)
    assert world.repo.users == {}
    assert world.signup(token).status_code == 200


@pytest.mark.parametrize(
    ("nickname", "ok"),
    [
        ("부산", True),
        ("  부산사람12  ", True),
        ("Busan2026", True),
        ("가" * 12, True),
        ("가", False),
        ("가" * 13, False),
        ("부산 사람", False),
        ("부산!", False),
        ("ㅂㅅ", False),  # 낱자모는 프론트 규칙(가-힣)처럼 받지 않는다
        ("１２", False),  # 전각 숫자
        ("", False),
    ],
)
def test_nickname_rules_match_the_app(world, nickname, ok):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    token = world.app_login().json()["data"]["signup_token"]
    r = world.signup(token, nickname=nickname)
    if ok:
        assert r.status_code == 200 and r.json()["data"]["user"]["nickname"] == nickname.strip()
    else:
        assert error(r) == (400, "VALIDATION_ERROR", "nickname")


def test_duplicate_nicknames_are_allowed(world):
    world.new_member(token="a", user_id="k-1", nickname="부산사람")
    assert world.new_member(token="b", user_id="k-2", nickname="부산사람")["user"]["nickname"] == "부산사람"


def test_signup_token_is_single_use(world):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    token = world.app_login().json()["data"]["signup_token"]
    assert world.signup(token).status_code == 200
    assert error(world.signup(token)) == (401, "UNAUTHORIZED", None)


def test_signup_token_expires_after_ten_minutes(world):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    token = world.app_login().json()["data"]["signup_token"]
    world.clock.advance(minutes=10)
    assert error(world.signup(token)) == (401, "UNAUTHORIZED", None)
    assert error(world.signup("made-up")) == (401, "UNAUTHORIZED", None)


def test_if_the_same_social_account_signed_up_meanwhile_signup_logs_into_it(world):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    t1 = world.app_login().json()["data"]["signup_token"]
    t2 = world.app_login().json()["data"]["signup_token"]
    first = world.signup(t1, nickname="먼저").json()["data"]
    second = world.signup(t2, nickname="나중").json()["data"]
    assert second["user"]["id"] == first["user"]["id"] and second["user"]["nickname"] == "먼저"
    assert len(world.repo.users) == 1


def test_agreements_must_all_be_sent(world):
    world.kakao.tokens["sdk-1"] = world.kakao.user("k-1")
    token = world.app_login().json()["data"]["signup_token"]
    r = world.signup(token, agreements={"terms": True, "privacy": True})
    assert error(r) == (400, "VALIDATION_ERROR", "age14")


# ── 1. 로그인 실패·설정 ──


def test_unknown_social_tokens_are_401(world):
    assert error(world.app_login(token="forged")) == (401, "UNAUTHORIZED", None)


def test_provider_outage_is_503(world):
    world.kakao.down = True
    assert error(world.app_login()) == (503, "SOURCE_UNAVAILABLE", None)


def test_unknown_provider_is_a_validation_error(world):
    assert error(world.app_login(provider="apple"))[:2] == (400, "VALIDATION_ERROR")


def test_app_login_needs_the_sdk_token(world):
    r = world.client.post(f"{API}/auth/oauth/kakao", json={})
    assert error(r) == (400, "VALIDATION_ERROR", "access_token")


@pytest.mark.parametrize("secret", ["", "short-secret"])
def test_without_a_strong_jwt_secret_auth_is_503(secret):
    with World(jwt_secret=secret) as w:
        w.kakao.tokens["sdk-1"] = w.kakao.user("k-1")
        assert error(w.app_login()) == (503, "SOURCE_UNAVAILABLE", None)
        assert error(w.me("x"))[:2] == (503, "SOURCE_UNAVAILABLE")


def test_a_provider_without_keys_is_503():
    w = World()
    del w.providers[SocialProvider.naver]
    with w:
        assert error(w.app_login(provider="naver")) == (503, "SOURCE_UNAVAILABLE", None)


def test_without_a_database_auth_is_503():
    app = create_app(settings=Settings(jwt_secret=SECRET), clock=lambda: FIXED_NOW, market_repo=empty_market())
    with TestClient(app, headers={"X-Device-Id": str(uuid4())}) as c:
        r = c.post(f"{API}/auth/oauth/kakao", json={"access_token": "t"})
        assert error(r) == (503, "SOURCE_UNAVAILABLE", None)


# ── 1. 로그인(웹) ──


def web_login(world, code="code-1", redirect_uri=CALLBACK, **extra):
    body = {"code": code, "redirect_uri": redirect_uri, "state": "st-1", **extra}
    return world.client.post(f"{API}/auth/oauth/kakao", json=body, headers=WEB)


def test_web_login_exchanges_the_code_and_puts_the_refresh_token_only_in_a_cookie(world):
    member = world.new_member(user_id="k-1")
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    r = web_login(world, code_verifier="v" * 43)
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data) == {"is_new_user", "access_token", "user"} and data["user"]["id"] == member["user"]["id"]
    assert world.kakao.exchanges == [("code-1", CALLBACK, "st-1", "v" * 43)]
    cookie = set_cookie(r)
    assert cookie.startswith("refresh_token=")
    attrs = {a.strip().lower() for a in cookie.split(";")[1:]}
    assert {"httponly", "secure", "samesite=lax", "path=/api/v1/auth", "max-age=1209600"} <= attrs
    assert r.json()["data"].get("refresh_token") is None


def test_web_redirect_uri_must_match_the_allow_list_exactly(world):
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    for uri in [CALLBACK + "/", CALLBACK + "?next=/", "https://evil.example.com/auth/callback", CALLBACK.upper()]:
        assert error(web_login(world, redirect_uri=uri)) == (400, "VALIDATION_ERROR", "redirect_uri")
    assert world.kakao.exchanges == []


@pytest.mark.parametrize("field", ["code", "redirect_uri", "state"])
def test_web_login_needs_code_redirect_uri_and_state(world, field):
    body = {"code": "c", "redirect_uri": CALLBACK, "state": "s"}
    del body[field]
    r = world.client.post(f"{API}/auth/oauth/kakao", json=body, headers=WEB)
    assert error(r) == (400, "VALIDATION_ERROR", field)


def test_a_used_or_forged_code_is_401(world):
    assert error(web_login(world, code="forged")) == (401, "UNAUTHORIZED", None)


def test_web_new_user_signup_sets_the_cookie(world):
    world.kakao.codes["code-1"] = world.kakao.user("k-1", None)
    first = web_login(world).json()["data"]
    assert first["is_new_user"] is True and first["profile"] == {"nickname": None}
    r = world.signup(first["signup_token"], **WEB)
    assert r.status_code == 200 and "refresh_token" not in r.json()["data"]
    assert set_cookie(r).startswith("refresh_token=")


def test_bad_client_header_is_rejected(world):
    r = world.client.post(f"{API}/auth/oauth/kakao", json={"access_token": "t"}, headers={"X-Client": "desktop"})
    assert error(r)[:2] == (400, "VALIDATION_ERROR")


# ── 4. 토큰 ──


def test_refresh_rotates_and_the_old_token_stops_working(world):
    session = world.new_member()
    world.clock.advance(days=1)
    r = world.refresh(session["refresh_token"])
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    data = r.json()["data"]
    assert set(data) == {"access_token", "refresh_token"} and data["refresh_token"] != session["refresh_token"]
    assert world.me(data["access_token"]).status_code == 200
    assert world.refresh(data["refresh_token"]).status_code == 200


def test_reusing_a_rotated_refresh_token_logs_out_every_session(world):
    session = world.new_member()
    other_device = world.app_login().json()["data"]["refresh_token"]
    rotated = world.refresh(session["refresh_token"]).json()["data"]["refresh_token"]
    world.clock.advance(seconds=30)  # 유예(기본 30초)가 지난 재사용
    assert error(world.refresh(session["refresh_token"])) == (401, "UNAUTHORIZED", None)
    assert error(world.refresh(rotated)) == (401, "UNAUTHORIZED", None)
    assert error(world.refresh(other_device)) == (401, "UNAUTHORIZED", None)


def test_a_duplicate_refresh_right_after_rotation_is_refused_without_logging_out(world):
    """두 탭이 거의 동시에 갱신한 경우: 늦은 쪽만 401, 새 토큰과 다른 세션은 그대로."""
    session = world.new_member()
    other_device = world.app_login().json()["data"]["refresh_token"]
    rotated = world.refresh(session["refresh_token"]).json()["data"]["refresh_token"]
    world.clock.advance(seconds=29)
    assert error(world.refresh(session["refresh_token"])) == (401, "UNAUTHORIZED", None)
    assert world.refresh(rotated).status_code == 200
    assert world.refresh(other_device).status_code == 200


def test_the_reuse_grace_is_a_setting():
    with World(refresh_reuse_grace_seconds=0) as w:
        session = w.new_member()
        rotated = w.refresh(session["refresh_token"]).json()["data"]["refresh_token"]
        assert w.refresh(session["refresh_token"]).status_code == 401
        assert w.refresh(rotated).status_code == 401


def test_refresh_tokens_expire_after_fourteen_days(world):
    session = world.new_member()
    world.clock.advance(days=14)
    assert error(world.refresh(session["refresh_token"])) == (401, "UNAUTHORIZED", None)


def test_unknown_refresh_token_is_401_and_a_missing_one_is_a_validation_error(world):
    assert error(world.refresh("made-up")) == (401, "UNAUTHORIZED", None)
    r = world.client.post(f"{API}/auth/refresh", json={})
    assert error(r) == (400, "VALIDATION_ERROR", "refresh_token")


def test_web_refresh_reads_the_cookie_only_with_the_web_header(world):
    world.new_member(user_id="k-1")
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    web_login(world)  # TestClient가 쿠키를 들고 있다
    # X-Client: web이 없으면 쿠키를 읽지 않는다(앱 방식으로 본문을 찾는다)
    assert error(world.client.post(f"{API}/auth/refresh"))[:2] == (400, "VALIDATION_ERROR")
    r = world.client.post(f"{API}/auth/refresh", headers=WEB)
    assert r.status_code == 200 and set(r.json()["data"]) == {"access_token"}
    assert set_cookie(r).startswith("refresh_token=")
    assert world.me(r.json()["data"]["access_token"]).status_code == 200


def test_web_refresh_without_a_cookie_is_401(world):
    assert error(world.client.post(f"{API}/auth/refresh", headers=WEB)) == (401, "UNAUTHORIZED", None)


def test_web_refresh_ignores_a_refresh_token_in_the_body(world):
    session = world.new_member()
    r = world.client.post(f"{API}/auth/refresh", json={"refresh_token": session["refresh_token"]}, headers=WEB)
    assert error(r) == (401, "UNAUTHORIZED", None)


def test_access_tokens_expire_after_thirty_minutes(world):
    session = world.new_member()
    world.clock.advance(minutes=29, seconds=59)
    assert world.me(session["access_token"]).status_code == 200
    world.clock.advance(seconds=1)
    assert error(world.me(session["access_token"])) == (401, "UNAUTHORIZED", None)


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer garbage", "Basic abc", "bearer x.y.z"])
def test_protected_endpoints_need_a_valid_bearer_token(world, header):
    headers = {} if header is None else {"Authorization": header}
    assert error(world.client.get(f"{API}/users/me", headers=headers)) == (401, "UNAUTHORIZED", None)


def test_bearer_scheme_is_case_insensitive(world):
    session = world.new_member()
    r = world.client.get(f"{API}/users/me", headers={"Authorization": f"bearer {session['access_token']}"})
    assert r.status_code == 200


# ── 5. 로그아웃 ──


def test_logout_revokes_that_refresh_token_only(world):
    session = world.new_member()
    other = world.app_login().json()["data"]["refresh_token"]
    r = world.client.post(f"{API}/auth/logout", json={"refresh_token": session["refresh_token"]})
    assert r.status_code == 204
    assert error(world.refresh(session["refresh_token"]))[0] == 401
    # 로그아웃한 토큰을 다시 쓰면 재사용으로 보아 다른 세션도 끊긴다
    assert error(world.refresh(other))[0] == 401


def test_logout_of_an_unknown_token_still_succeeds(world):
    assert world.client.post(f"{API}/auth/logout", json={"refresh_token": "nope"}).status_code == 204
    assert world.client.post(f"{API}/auth/logout", json={}).status_code == 204


def test_web_logout_revokes_the_cookie_token_and_clears_the_cookie(world):
    world.new_member(user_id="k-1")
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    web_login(world)
    r = world.client.post(f"{API}/auth/logout", headers=WEB)
    assert r.status_code == 204
    cookie = set_cookie(r).lower()
    assert cookie.startswith('refresh_token=""') and "max-age=0" in cookie and "path=/api/v1/auth" in cookie
    assert error(world.client.post(f"{API}/auth/refresh", headers=WEB))[0] == 401


# ── 6. 내 정보 ──


def patch_me(world, session, body):
    return world.client.patch(
        f"{API}/users/me", json=body, headers={"Authorization": f"Bearer {session['access_token']}"}
    )


def test_patch_nickname_and_settings(world):
    session = world.new_member()
    r = patch_me(world, session, {"nickname": " 해운대 ", "settings": {"loading_tips": False}})
    assert r.status_code == 200
    user = r.json()["data"]
    assert user["nickname"] == "해운대" and user["settings"] == {
        "loading_tips": False,
        "unseen_term_dots": True,
        "weekly_report": True,
    }
    user = patch_me(world, session, {"settings": {"unseen_term_dots": False}}).json()["data"]
    assert user["nickname"] == "해운대" and user["settings"] == {
        "loading_tips": False,
        "unseen_term_dots": False,
        "weekly_report": True,
    }
    assert world.me(session["access_token"]).json()["data"] == user


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"nickname": "부산 사람"}, "nickname"),
        ({"nickname": "가"}, "nickname"),
        ({"settings": {"dark_mode": True}}, "dark_mode"),
        ({"settings": {"loading_tips": "maybe"}}, "loading_tips"),
    ],
)
def test_patch_validates(world, body, field):
    session = world.new_member()
    assert error(patch_me(world, session, body)) == (400, "VALIDATION_ERROR", field)


# ── 7. 탈퇴 ──


def test_withdraw_anonymizes_and_ends_every_session(world):
    session = world.new_member(marketing=True)
    user_id = session["user"]["id"]
    r = world.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert r.status_code == 204
    assert error(world.me(session["access_token"])) == (401, "UNAUTHORIZED", None)
    assert error(world.refresh(session["refresh_token"]))[0] == 401
    raw = world.repo.users[next(iter(world.repo.users))]
    assert (raw.nickname, raw.marketing_push, raw.marketing_consented_at) == ("탈퇴한 사용자", False, None)
    assert world.repo.identities == {}
    # 같은 소셜 계정으로 다시 오면 처음 온 사람이다
    again = world.app_login().json()["data"]
    assert again["is_new_user"] is True
    assert world.signup(again["signup_token"]).json()["data"]["user"]["id"] != user_id


def test_web_withdraw_clears_the_cookie(world):
    session = world.new_member()
    r = world.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}", **WEB})
    assert r.status_code == 204 and "max-age=0" in set_cookie(r).lower()


# ── 비밀값 ──


def test_tokens_and_codes_never_reach_the_logs(world, caplog):
    caplog.set_level(logging.DEBUG)
    session = world.new_member(token="sdk-secret-token")
    world.kakao.codes["code-secret"] = world.kakao.user("k-1")
    web_login(world, code="code-secret", code_verifier="verifier-secret")
    rotated = world.refresh(session["refresh_token"]).json()["data"]
    world.refresh(session["refresh_token"])  # 재사용 감지는 경고를 남긴다
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for secret in [
        "sdk-secret-token",
        "code-secret",
        "verifier-secret",
        session["refresh_token"],
        session["access_token"],
        rotated["refresh_token"],
        SECRET,
    ]:
        assert secret not in logged


# ── 앱 토큰은 우리 앱에 발급된 것만 ──


def test_naver_app_tokens_are_refused_and_only_the_web_code_flow_is_accepted(world):
    naver = world.providers[SocialProvider.naver]
    naver.tokens["nv-sdk"] = naver.user("nv-1")
    r = world.app_login(provider="naver", token="nv-sdk")
    assert error(r) == (400, "VALIDATION_ERROR", "access_token")
    assert "웹" in r.json()["error"]["message"]
    naver.codes["nv-code"] = naver.user("nv-1")
    body = {"code": "nv-code", "redirect_uri": CALLBACK, "state": "s"}
    r = world.client.post(f"{API}/auth/oauth/naver", json=body, headers=WEB)
    assert r.status_code == 200 and r.json()["data"]["is_new_user"] is True


def real_provider_world(**settings) -> TestClient:
    """제공자는 실제 어댑터(설정으로 조립). 네트워크에 닿기 전에 끝나는 경우만 시험한다."""
    app = create_app(
        settings=Settings(jwt_secret=SECRET, auth_redirect_uris=[CALLBACK], **settings),
        clock=lambda: FIXED_NOW,
        account_repo=InMemoryAccountRepository(),
        market_repo=empty_market(),
    )
    return TestClient(app, headers={"X-Device-Id": str(uuid4())})


def test_kakao_app_login_without_the_app_id_setting_is_503():
    with real_provider_world(kakao_rest_key="rest-key") as c:
        r = c.post(f"{API}/auth/oauth/kakao", json={"access_token": "sdk"})
        assert error(r) == (503, "SOURCE_UNAVAILABLE", None)


def test_naver_app_login_is_400_even_with_real_keys():
    with real_provider_world(naver_client_id="id", naver_client_secret="sec") as c:
        r = c.post(f"{API}/auth/oauth/naver", json={"access_token": "sdk"})
        assert error(r) == (400, "VALIDATION_ERROR", "access_token")


def test_google_app_client_ids_alone_do_not_enable_web_login():
    with real_provider_world(google_client_ids=["ios.apps.googleusercontent.com"]) as c:
        body = {"code": "c", "redirect_uri": CALLBACK, "state": "s", "code_verifier": "v" * 43}
        r = c.post(f"{API}/auth/oauth/google", json=body, headers=WEB)
        assert error(r) == (503, "SOURCE_UNAVAILABLE", None)


# ── 보안 리뷰 반영 ──


def test_web_refresh_401_also_clears_the_cookie(world):
    world.client.cookies.set("refresh_token", "stale", domain="testserver.local", path="/api/v1/auth")
    r = world.client.post(f"{API}/auth/refresh", headers=WEB)
    assert error(r) == (401, "UNAUTHORIZED", None)
    assert "max-age=0" in set_cookie(r).lower()


def test_web_login_without_any_allowed_redirect_uri_is_503():
    with World(auth_redirect_uris=[]) as w:
        w.kakao.codes["code-1"] = w.kakao.user("k-1")
        assert error(web_login(w)) == (503, "SOURCE_UNAVAILABLE", None)
        assert w.kakao.exchanges == []


def test_only_https_redirect_uris_are_allowed(caplog):
    http_uri, scheme_uri = "http://pugurin.netlify.app/auth/callback", "pugurin://auth/callback"
    with World(auth_redirect_uris=[CALLBACK, http_uri, scheme_uri]) as w:
        for uri in (http_uri, scheme_uri):
            assert error(web_login(w, redirect_uri=uri)) == (400, "VALIDATION_ERROR", "redirect_uri")
        w.kakao.codes["code-1"] = w.kakao.user("k-1")
        assert web_login(w).status_code == 200
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(http_uri in m for m in warned) and any(scheme_uri in m for m in warned)


def test_google_web_login_requires_pkce(world):
    google = world.providers[SocialProvider.google]
    google.codes["g-code"] = google.user("g-1")
    body = {"code": "g-code", "redirect_uri": CALLBACK, "state": "s"}
    r = world.client.post(f"{API}/auth/oauth/google", json=body, headers=WEB)
    assert error(r) == (400, "VALIDATION_ERROR", "code_verifier")
    assert google.exchanges == []
    r = world.client.post(f"{API}/auth/oauth/google", json={**body, "code_verifier": "v" * 43}, headers=WEB)
    assert r.status_code == 200


def test_kakao_pkce_stays_optional(world):
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    assert web_login(world).status_code == 200


def test_withdraw_unlinks_the_kakao_account_after_commit(world):
    session = world.new_member(user_id="k-77")
    r = world.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert r.status_code == 204
    assert world.unlinker.unlinked == ["k-77"]
    # 끊기 전에 탈퇴는 이미 커밋됐다
    assert world.unlinker.saw_identity_deleted == [True]


def test_withdraw_still_succeeds_when_unlinking_fails(world, caplog):
    world.unlinker.fail = True
    session = world.new_member(user_id="k-77")
    r = world.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert r.status_code == 204 and world.repo.identities == {}
    logged = "\n".join(m.getMessage() for m in caplog.records if m.levelname == "WARNING")
    assert "연결 끊기" in logged and "k-77" not in logged


def test_withdraw_without_an_unlinker_only_warns(caplog):
    w = World()
    w.client.app.state.container.auth_service._unlinkers = {}
    with w:
        session = w.new_member(user_id="k-77")
        r = w.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
        assert r.status_code == 204
    assert any("PUGURIN_KAKAO_ADMIN_KEY" in m.getMessage() for m in caplog.records)


def test_naver_and_google_withdrawals_do_not_try_to_unlink(world):
    google = world.providers[SocialProvider.google]
    google.tokens["g-sdk"] = google.user("g-1")
    token = world.app_login(provider="google", token="g-sdk").json()["data"]["signup_token"]
    session = world.signup(token).json()["data"]
    r = world.client.delete(f"{API}/users/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert r.status_code == 204 and world.unlinker.unlinked == []


def test_a_late_duplicate_web_refresh_does_not_clear_the_new_cookie(world):
    """탭 A가 먼저 갱신해 새 쿠키를 받고, 같은 옛 쿠키로 보낸 탭 B의 401이 늦게 와도 A의 새 쿠키를 지우지 않는다."""
    world.new_member(user_id="k-1")
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    old = set_cookie(web_login(world)).split(";")[0].split("=", 1)[1]
    a = world.client.post(f"{API}/auth/refresh", headers=WEB)
    assert a.status_code == 200
    b = world.client.post(f"{API}/auth/refresh", headers={**WEB, "Cookie": f"refresh_token={old}"})
    assert error(b) == (401, "UNAUTHORIZED", None)
    assert "set-cookie" not in b.headers
    # A가 받은 새 쿠키로 다음 갱신이 된다
    assert world.client.post(f"{API}/auth/refresh", headers=WEB).status_code == 200


def test_web_refresh_reuse_outside_the_grace_still_clears_the_cookie(world):
    world.new_member(user_id="k-1")
    world.kakao.codes["code-1"] = world.kakao.user("k-1")
    old = set_cookie(web_login(world)).split(";")[0].split("=", 1)[1]
    assert world.client.post(f"{API}/auth/refresh", headers=WEB).status_code == 200
    world.clock.advance(seconds=30)
    b = world.client.post(f"{API}/auth/refresh", headers={**WEB, "Cookie": f"refresh_token={old}"})
    assert error(b)[0] == 401 and "max-age=0" in set_cookie(b).lower()

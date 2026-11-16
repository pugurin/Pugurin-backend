"""소셜 로그인 어댑터: 카카오·네이버·구글 공식 엔드포인트를 부르는 모양과 오류 구분(네트워크 없이 MockTransport)."""

import json
import time
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters.oauth import (
    GoogleOAuth,
    KakaoOAuth,
    NaverOAuth,
    ProviderNotConfigured,
    SocialAuthRejected,
)
from app.repositories.ports import SourceUnavailable
from app.repositories.types import SocialProfile, SocialProvider

pytestmark = pytest.mark.anyio

REDIRECT = "https://pugurin.netlify.app/auth/callback"


def mock_http(handler) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(record)), seen


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# ── 카카오 ──

KAKAO_ME = {"id": 4012345678, "kakao_account": {"profile": {"nickname": "부산갈매기"}}}


async def test_kakao_token_is_checked_with_the_user_info_api():
    http, seen = mock_http(lambda r: httpx.Response(200, json=KAKAO_ME))
    profile = await KakaoOAuth("rest", "", http=http).verify_token("sdk-token")
    assert profile == SocialProfile(SocialProvider.kakao, "4012345678", "부산갈매기")
    [req] = seen
    assert (req.method, str(req.url)) == ("GET", "https://kapi.kakao.com/v2/user/me")
    assert req.headers["Authorization"] == "Bearer sdk-token"


async def test_kakao_nickname_falls_back_to_properties_or_none():
    http, _ = mock_http(lambda r: httpx.Response(200, json={"id": 1, "properties": {"nickname": " 해운대 "}}))
    assert (await KakaoOAuth("rest", "", http=http).verify_token("t")).nickname == "해운대"
    http, _ = mock_http(lambda r: httpx.Response(200, json={"id": 1}))
    assert (await KakaoOAuth("rest", "", http=http).verify_token("t")).nickname is None


async def test_kakao_code_is_exchanged_with_secret_and_pkce_then_user_info():
    def handler(request):
        if request.url.host == "kauth.kakao.com":
            return httpx.Response(200, json={"access_token": "kakao-at", "token_type": "bearer"})
        assert request.headers["Authorization"] == "Bearer kakao-at"
        return httpx.Response(200, json=KAKAO_ME)

    http, seen = mock_http(handler)
    profile = await KakaoOAuth("rest", "sec", http=http).exchange_code("the-code", REDIRECT, "st", "verifier")
    assert profile.provider_user_id == "4012345678"
    token_req = seen[0]
    assert (token_req.method, str(token_req.url)) == ("POST", "https://kauth.kakao.com/oauth/token")
    assert form(token_req) == {
        "grant_type": "authorization_code",
        "client_id": "rest",
        "client_secret": "sec",
        "redirect_uri": REDIRECT,
        "code": "the-code",
        "code_verifier": "verifier",
    }


async def test_kakao_exchange_leaves_out_an_unset_secret_and_verifier():
    def handler(request):
        if request.url.host == "kauth.kakao.com":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json=KAKAO_ME)

    http, seen = mock_http(handler)
    await KakaoOAuth("rest", "", http=http).exchange_code("c", REDIRECT, "st", None)
    assert set(form(seen[0])) == {"grant_type", "client_id", "redirect_uri", "code"}


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_kakao_rejected_tokens_are_social_auth_failures(status):
    http, _ = mock_http(
        lambda r: httpx.Response(status, json={"code": -401, "msg": "this access token does not exist"})
    )
    with pytest.raises(SocialAuthRejected):
        await KakaoOAuth("rest", "", http=http).verify_token("bad")


async def test_a_bad_code_is_rejected_but_a_bad_client_secret_is_our_problem():
    http, _ = mock_http(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(SocialAuthRejected):
        await KakaoOAuth("rest", "sec", http=http).exchange_code("c", REDIRECT, "st", None)
    http, _ = mock_http(lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(SourceUnavailable) as e:
        await KakaoOAuth("rest", "sec", http=http).exchange_code("c", REDIRECT, "st", None)
    assert not isinstance(e.value, SocialAuthRejected)


async def test_provider_outages_are_source_unavailable_and_never_echo_the_token():
    http, _ = mock_http(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(SourceUnavailable) as e:
        await KakaoOAuth("rest", "", http=http).verify_token("secret-sdk-token")
    assert "secret-sdk-token" not in str(e.value)

    def boom(request):
        raise httpx.ConnectError("down")

    http, _ = mock_http(boom)
    with pytest.raises(SourceUnavailable):
        await KakaoOAuth("rest", "", http=http).verify_token("t")


async def test_a_user_info_answer_without_an_id_is_rejected():
    http, _ = mock_http(lambda r: httpx.Response(200, json={"properties": {}}))
    with pytest.raises(SocialAuthRejected):
        await KakaoOAuth("rest", "", http=http).verify_token("t")


# ── 네이버 ──

NAVER_ME = {
    "resultcode": "00",
    "message": "success",
    "response": {"id": "nv-abc", "nickname": "광안리", "name": "실명"},
}


async def test_naver_token_is_checked_with_the_profile_api_and_uses_the_nickname_not_the_real_name():
    http, seen = mock_http(lambda r: httpx.Response(200, json=NAVER_ME))
    profile = await NaverOAuth("id", "sec", http=http).verify_token("sdk")
    assert profile == SocialProfile(SocialProvider.naver, "nv-abc", "광안리")
    assert str(seen[0].url) == "https://openapi.naver.com/v1/nid/me"
    assert seen[0].headers["Authorization"] == "Bearer sdk"


async def test_naver_failure_result_codes_are_rejected():
    http, _ = mock_http(lambda r: httpx.Response(200, json={"resultcode": "024", "message": "Authentication failed"}))
    with pytest.raises(SocialAuthRejected):
        await NaverOAuth("id", "sec", http=http).verify_token("bad")


async def test_naver_code_exchange_posts_state_but_no_verifier():
    def handler(request):
        if request.url.host == "nid.naver.com":
            return httpx.Response(200, json={"access_token": "nv-at"})
        return httpx.Response(200, json=NAVER_ME)

    http, seen = mock_http(handler)
    await NaverOAuth("id", "sec", http=http).exchange_code("c", REDIRECT, "st", "verifier")
    assert (seen[0].method, str(seen[0].url)) == ("POST", "https://nid.naver.com/oauth2.0/token")
    assert form(seen[0]) == {
        "grant_type": "authorization_code",
        "client_id": "id",
        "client_secret": "sec",
        "code": "c",
        "state": "st",
    }


async def test_naver_reports_a_bad_code_with_http_200_and_an_error_field():
    http, _ = mock_http(lambda r: httpx.Response(200, json={"error": "invalid_request", "error_description": "no"}))
    with pytest.raises(SocialAuthRejected):
        await NaverOAuth("id", "sec", http=http).exchange_code("c", REDIRECT, "st", None)


# ── 구글 ──

CLIENT_ID = "web-client.apps.googleusercontent.com"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_of(key, kid="k1") -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**jwk, "kid": kid, "alg": "RS256", "use": "sig"}]}


def id_token(key, kid="k1", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "g-123",
        "name": "구글 이름",
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def google_with_jwks(key, **kw):
    def handler(request):
        assert str(request.url) == "https://www.googleapis.com/oauth2/v3/certs"
        return httpx.Response(200, json=jwks_of(key))

    http, seen = mock_http(handler)
    return GoogleOAuth(CLIENT_ID, kw.get("secret", ""), http=http), seen


async def test_google_id_token_is_verified_with_googles_keys(rsa_key):
    google, seen = google_with_jwks(rsa_key)
    profile = await google.verify_token(id_token(rsa_key))
    assert profile == SocialProfile(SocialProvider.google, "g-123", "구글 이름")
    # 키는 다시 받지 않는다
    await google.verify_token(id_token(rsa_key, iss="accounts.google.com"))
    assert len(seen) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-elses-app.apps.googleusercontent.com"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 600},
    ],
)
async def test_google_id_tokens_for_other_apps_issuers_or_expired_are_rejected(rsa_key, overrides):
    google, _ = google_with_jwks(rsa_key)
    with pytest.raises(SocialAuthRejected):
        await google.verify_token(id_token(rsa_key, **overrides))


async def test_google_id_token_signed_by_another_key_is_rejected(rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    google, _ = google_with_jwks(rsa_key)
    with pytest.raises(SocialAuthRejected):
        await google.verify_token(id_token(other))
    with pytest.raises(SocialAuthRejected):
        await google.verify_token(id_token(other, kid="unknown"))


async def test_google_access_token_uses_userinfo():
    http, seen = mock_http(lambda r: httpx.Response(200, json={"sub": "g-9", "name": "이름"}))
    profile = await GoogleOAuth(CLIENT_ID, "", http=http).verify_token("ya29.access")
    assert profile == SocialProfile(SocialProvider.google, "g-9", "이름")
    assert str(seen[0].url) == "https://openidconnect.googleapis.com/v1/userinfo"
    assert seen[0].headers["Authorization"] == "Bearer ya29.access"


async def test_google_code_exchange_needs_the_client_secret():
    http, seen = mock_http(lambda r: httpx.Response(500))
    with pytest.raises(ProviderNotConfigured):
        await GoogleOAuth(CLIENT_ID, "", http=http).exchange_code("c", REDIRECT, "st", None)
    assert seen == []


async def test_google_code_exchange_posts_pkce_then_userinfo():
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "g-at", "id_token": "x.y.z"})
        assert request.headers["Authorization"] == "Bearer g-at"
        return httpx.Response(200, json={"sub": "g-1", "name": None})

    http, seen = mock_http(handler)
    profile = await GoogleOAuth(CLIENT_ID, "gsec", http=http).exchange_code("c", REDIRECT, "st", "ver")
    assert profile == SocialProfile(SocialProvider.google, "g-1", None)
    assert (seen[0].method, str(seen[0].url)) == ("POST", "https://oauth2.googleapis.com/token")
    assert form(seen[0]) == {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": "gsec",
        "redirect_uri": REDIRECT,
        "code": "c",
        "code_verifier": "ver",
    }

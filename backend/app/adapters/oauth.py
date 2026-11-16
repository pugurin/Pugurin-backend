"""소셜 로그인 확인: 카카오·네이버·구글 공식 API로 앱 토큰을 확인하거나 웹 인가 코드를 토큰으로 바꾼다.

- 토큰·인가 코드·시크릿은 로그와 예외 메시지에 넣지 않는다. 시크릿은 URL이 아니라 POST 본문으로만 보낸다.
- 토큰·코드가 틀리면 SocialAuthRejected(→ 401), 제공자 장애나 우리 설정 오류는 SourceUnavailable(→ 503).
- 앱 SDK 토큰은 **우리 앱에 발급된 것만** 받는다(다른 앱이 받은 사용자 토큰으로 로그인하는 토큰 대체 공격 방지).
  카카오는 토큰 정보의 app_id, 구글은 ID 토큰 aud 또는 tokeninfo의 aud·azp로 확인한다. 네이버는 확인할 공식 방법을
  찾지 못해 앱 토큰을 받지 않는다(웹 인가 코드만). 웹 인가 코드는 우리 시크릿으로 바꾸므로 따로 확인하지 않는다.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Protocol

import httpx
import jwt

from app.repositories.ports import SourceUnavailable
from app.repositories.types import SocialProfile, SocialProvider

logger = logging.getLogger(__name__)

# 우리 앱 설정(클라이언트 ID·시크릿·redirect_uri 등록)이 틀렸을 때 제공자가 주는 오류. 사용자 탓이 아니다
_CLIENT_ERRORS = {"invalid_client", "unauthorized_client", "invalid_scope", "unsupported_grant_type"}


class SocialAuthRejected(Exception):
    """제공자가 토큰·인가 코드를 받아 주지 않았다."""


class ProviderNotConfigured(SourceUnavailable):
    """그 제공자의 키가 서버에 없다."""


class SocialUnlinker(Protocol):
    """탈퇴할 때 제공자 쪽 앱 연결을 끊는다. 서버가 제공자 토큰을 저장하지 않아도 되는 제공자만(지금은 카카오)."""

    async def unlink(self, provider_user_id: str) -> None: ...

    async def aclose(self) -> None: ...


class AppTokenNotSupported(Exception):
    """이 제공자는 앱 토큰이 우리 앱 것인지 확인할 수 없어 앱 토큰 로그인을 받지 않는다(→ 400)."""


class _RedactTokenQuery(logging.Filter):
    """httpx는 요청 URL을 INFO 로그로 남긴다. 구글 tokeninfo처럼 URL에 토큰이 실리는 호출은 값을 가린다."""

    _PATTERN = re.compile(r"(access_token=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._PATTERN.sub(r"\1[REDACTED]", str(a)) if "access_token=" in str(a) else a for a in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_RedactTokenQuery())


class OAuthProvider(Protocol):
    async def verify_token(self, token: str) -> SocialProfile:
        """앱: 소셜 SDK가 준 토큰을 확인한다."""
        ...

    async def exchange_code(self, code: str, redirect_uri: str, state: str, code_verifier: str | None) -> SocialProfile:
        """웹: 인가 코드를 토큰으로 바꾼 뒤 사용자 정보를 얻는다. redirect_uri는 허용 목록으로 이미 확인했다."""
        ...

    async def aclose(self) -> None: ...


def _clean(name) -> str | None:
    return (name.strip() or None) if isinstance(name, str) else None


class _OAuthHttp:
    """공통 호출. 재시도하지 않는다(로그인은 사용자가 기다리는 요청이고 인가 코드는 1회용이다)."""

    def __init__(self, name: str, http: httpx.AsyncClient | None):
        self.name = name
        self._http = http or httpx.AsyncClient(timeout=10)

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{self.name} 로그인 서버에 닿지 않습니다({type(e).__name__})") from None

    async def user_info(self, url: str, token: str) -> dict:
        response = await self._send("GET", url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code in (400, 401, 403):
            raise SocialAuthRejected(f"{self.name} 토큰 확인 실패(HTTP {response.status_code})")
        return self._json(response)

    async def token(self, url: str, data: dict[str, str]) -> str:
        """인가 코드 → 제공자 access token."""
        response = await self._send("POST", url, data=data)
        body = self._json(response, allow_client_error=True)
        error = body.get("error")
        if error in _CLIENT_ERRORS:
            logger.warning(
                "%s 토큰 교환이 클라이언트 설정 오류로 거절됐습니다(%s). 키·redirect_uri 등록을 확인하세요",
                self.name,
                error,
            )
            raise SourceUnavailable(f"{self.name} 로그인 설정 오류")
        access_token = body.get("access_token")
        if error or response.status_code >= 400 or not isinstance(access_token, str) or not access_token:
            raise SocialAuthRejected(f"{self.name} 인가 코드 교환 실패({error or response.status_code})")
        return access_token

    def _json(self, response: httpx.Response, *, allow_client_error: bool = False) -> dict:
        code = response.status_code
        if code >= 500 or (code >= 400 and not allow_client_error):
            raise SourceUnavailable(f"{self.name} 로그인 서버 오류(HTTP {code})")
        try:
            body = response.json()
        except ValueError:
            raise SourceUnavailable(f"{self.name} 응답이 JSON이 아닙니다") from None
        if not isinstance(body, dict):
            raise SourceUnavailable(f"{self.name} 응답 형식이 다릅니다")
        return body

    async def aclose(self) -> None:
        await self._http.aclose()


def _profile(provider: SocialProvider, user_id, nickname) -> SocialProfile:
    if user_id is None or (isinstance(user_id, str) and not user_id.strip()) or isinstance(user_id, bool):
        raise SocialAuthRejected(f"{provider.value} 사용자 ID가 없습니다")
    return SocialProfile(provider, str(user_id).strip(), _clean(nickname))


class KakaoOAuth(_OAuthHttp):
    """카카오 로그인. client_id는 REST API 키, 클라이언트 시크릿은 콘솔에서 켰을 때만 보낸다.
    앱 토큰은 토큰 정보 API의 app_id가 우리 앱 ID(PUGURIN_KAKAO_APP_ID)일 때만 받는다.
    앱 ID가 없으면 앱 로그인은 503(웹 인가 코드 로그인은 된다)."""

    ME = "https://kapi.kakao.com/v2/user/me"
    TOKEN_INFO = "https://kapi.kakao.com/v1/user/access_token_info"
    TOKEN = "https://kauth.kakao.com/oauth/token"

    def __init__(self, rest_key: str, client_secret: str, *, app_id: str = "", http: httpx.AsyncClient | None = None):
        super().__init__("카카오", http)
        self._client_id, self._secret, self._app_id = rest_key, client_secret, app_id.strip()

    async def verify_token(self, token: str) -> SocialProfile:
        if not self._app_id:
            raise ProviderNotConfigured("카카오 앱 로그인 설정(PUGURIN_KAKAO_APP_ID)이 없습니다")
        info = await self.user_info(self.TOKEN_INFO, token)
        if str(info.get("app_id")) != self._app_id:
            raise SocialAuthRejected("다른 앱에 발급된 카카오 토큰입니다")
        return await self._me(token)

    async def _me(self, token: str) -> SocialProfile:
        body = await self.user_info(self.ME, token)
        account = body.get("kakao_account") or {}
        nickname = (account.get("profile") or {}).get("nickname") or (body.get("properties") or {}).get("nickname")
        return _profile(SocialProvider.kakao, body.get("id"), nickname)

    async def exchange_code(self, code: str, redirect_uri: str, state: str, code_verifier: str | None) -> SocialProfile:
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if self._secret:
            data["client_secret"] = self._secret
        if code_verifier:
            data["code_verifier"] = code_verifier
        return await self._me(await self.token(self.TOKEN, data))


class KakaoUnlink(_OAuthHttp):
    """카카오 연결 끊기(어드민 키). 탈퇴한 사용자의 카카오 회원번호로 우리 앱과의 연결을 끊는다."""

    UNLINK = "https://kapi.kakao.com/v1/user/unlink"

    def __init__(self, admin_key: str, *, http: httpx.AsyncClient | None = None):
        super().__init__("카카오", http)
        self._admin_key = admin_key

    async def unlink(self, provider_user_id: str) -> None:
        response = await self._send(
            "POST",
            self.UNLINK,
            headers={"Authorization": f"KakaoAK {self._admin_key}"},
            data={"target_id_type": "user_id", "target_id": provider_user_id},
        )
        if response.status_code >= 400:
            raise SourceUnavailable(f"카카오 연결 끊기 실패(HTTP {response.status_code})")


class NaverOAuth(_OAuthHttp):
    """네이버 로그인(웹 인가 코드만). 이름(name)은 실명이라 쓰지 않고 별명(nickname)만 쓴다. PKCE는 지원하지 않는다.
    앱 토큰이 우리 앱에 발급된 것인지 확인할 공식 API를 확인하지 못해 앱 토큰은 받지 않는다."""

    ME = "https://openapi.naver.com/v1/nid/me"
    TOKEN = "https://nid.naver.com/oauth2.0/token"

    def __init__(self, client_id: str, client_secret: str, *, http: httpx.AsyncClient | None = None):
        super().__init__("네이버", http)
        self._client_id, self._secret = client_id, client_secret

    async def verify_token(self, token: str) -> SocialProfile:
        raise AppTokenNotSupported("네이버는 앱 토큰 로그인을 받지 않습니다")

    async def _me(self, token: str) -> SocialProfile:
        body = await self.user_info(self.ME, token)
        if body.get("resultcode") != "00":
            raise SocialAuthRejected(f"네이버 토큰 확인 실패({body.get('resultcode')})")
        response = body.get("response") or {}
        return _profile(SocialProvider.naver, response.get("id"), response.get("nickname"))

    async def exchange_code(self, code: str, redirect_uri: str, state: str, code_verifier: str | None) -> SocialProfile:
        # 네이버 토큰 API는 state를 필수로 받는다(검증은 웹이 콜백에서 이미 했다)
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._secret,
            "code": code,
            "state": state,
        }
        return await self._me(await self.token(self.TOKEN, data))


class GoogleOAuth(_OAuthHttp):
    """구글 로그인. 앱 SDK가 ID 토큰(JWT)을 주면 구글 공개키로 서명·aud·iss·만료를 확인하고,
    access token이면 tokeninfo의 aud·azp가 우리 클라이언트인지 본 뒤 userinfo를 부른다.
    우리 클라이언트 = 웹 클라이언트 ID(client_id) + 앱 클라이언트 ID 목록(audiences).
    웹 인가 코드 교환에는 웹 클라이언트 ID와 시크릿이 있어야 한다."""

    TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
    USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
    TOKEN = "https://oauth2.googleapis.com/token"
    CERTS = "https://www.googleapis.com/oauth2/v3/certs"
    ISSUERS = ("https://accounts.google.com", "accounts.google.com")
    KEYS_TTL = 3600.0
    REFETCH_MIN_INTERVAL = 60.0  # 모르는 kid가 계속 와도 키를 1분에 한 번만 다시 받는다

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        audiences: list[str] | tuple[str, ...] = (),
        http: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        super().__init__("구글", http)
        self._client_id, self._secret = client_id, client_secret
        self._audiences = sorted({a.strip() for a in (client_id, *audiences) if a.strip()})
        self._monotonic = monotonic
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    async def verify_token(self, token: str) -> SocialProfile:
        if not self._audiences:
            raise ProviderNotConfigured("구글 클라이언트 ID가 없습니다")
        if token.count(".") == 2:
            return await self._verify_id_token(token)
        # tokeninfo는 토큰을 쿼리로 받는다(공식 사용법). httpx 로그에서는 _RedactTokenQuery가 가린다
        response = await self._send("GET", self.TOKENINFO, params={"access_token": token})
        if response.status_code in (400, 401, 403):
            raise SocialAuthRejected(f"구글 토큰 확인 실패(HTTP {response.status_code})")
        info = self._json(response)
        if info.get("aud") not in self._audiences and info.get("azp") not in self._audiences:
            raise SocialAuthRejected("다른 앱에 발급된 구글 토큰입니다")
        body = await self.user_info(self.USERINFO, token)
        if info.get("sub") and str(info["sub"]) != str(body.get("sub")):
            raise SocialAuthRejected("구글 토큰 정보와 사용자 정보가 다릅니다")
        return _profile(SocialProvider.google, body.get("sub"), None)

    async def exchange_code(self, code: str, redirect_uri: str, state: str, code_verifier: str | None) -> SocialProfile:
        if not self._client_id or not self._secret:
            raise ProviderNotConfigured("구글 웹 로그인 키(PUGURIN_GOOGLE_CLIENT_ID·SECRET)가 없습니다")
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "client_secret": self._secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        access_token = await self.token(self.TOKEN, data)
        body = await self.user_info(self.USERINFO, access_token)
        return _profile(SocialProvider.google, body.get("sub"), None)

    async def _verify_id_token(self, token: str) -> SocialProfile:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise SocialAuthRejected("구글 ID 토큰 형식이 틀립니다") from None
        key = await self._key(kid)
        if key is None:
            raise SocialAuthRejected("구글 ID 토큰 서명 키를 찾을 수 없습니다")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self._audiences,
                issuer=self.ISSUERS,
                options={"require": ["iss", "aud", "sub", "exp", "iat"]},
                leeway=30,
            )
        except jwt.PyJWTError as e:
            raise SocialAuthRejected(f"구글 ID 토큰 확인 실패({type(e).__name__})") from None
        return _profile(SocialProvider.google, claims.get("sub"), None)

    async def _key(self, kid) -> jwt.PyJWK | None:
        now = self._monotonic()
        stale = self._fetched_at is None or now - self._fetched_at >= self.KEYS_TTL
        may_refetch = self._fetched_at is None or now - self._fetched_at >= self.REFETCH_MIN_INTERVAL
        unknown = kid not in self._keys and may_refetch
        if stale or unknown:
            response = await self._send("GET", self.CERTS)
            body = self._json(response)
            try:
                keys = jwt.PyJWKSet.from_dict(body).keys
            except jwt.PyJWTError:
                raise SourceUnavailable("구글 공개키 형식이 다릅니다") from None
            self._keys = {k.key_id: k for k in keys if k.key_id}
            self._fetched_at = now
        return self._keys.get(kid)

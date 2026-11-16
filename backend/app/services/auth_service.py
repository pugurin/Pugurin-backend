"""로그인·가입·토큰 갱신·로그아웃·내 정보·탈퇴 (F-AUTH-01~05). 설계: docs/AUTH.md

토큰 자체는 로그에 남기지 않는다(사용자 ID만).
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.adapters.oauth import AppTokenNotSupported, OAuthProvider, SocialAuthRejected
from app.auth.tokens import (
    REFRESH_TTL,
    SIGNUP_TTL,
    InvalidToken,
    JwtSigner,
    hash_token,
    new_opaque_token,
)
from app.core.dates import Clock
from app.core.errors import source_unavailable, unauthorized, validation_error
from app.repositories.ports import AccountRepository, SourceUnavailable
from app.repositories.types import Account, RotateOutcome, SignupClaim, SocialProfile, SocialProvider

logger = logging.getLogger(__name__)

NICKNAME_MIN, NICKNAME_MAX = 2, 12
# 프론트(authTypes.ts)와 같은 규칙: 완성형 한글·영문·숫자만
_NICKNAME = re.compile(r"[가-힣a-zA-Z0-9]+")
REQUIRED_AGREEMENTS = ("terms", "privacy", "age14")

# 앱 SDK 토큰을 받는 제공자. 네이버는 토큰이 우리 앱 것인지 확인할 공식 방법이 없어 웹 인가 코드만 받는다
APP_TOKEN_PROVIDERS = frozenset({SocialProvider.kakao, SocialProvider.google})
APP_TOKEN_REFUSED = "이 소셜 계정은 앱 토큰으로 로그인할 수 없어요. 웹 로그인(인가 코드)으로 시도해 주세요"

NOT_CONFIGURED = "로그인을 아직 쓸 수 없어요. 잠시 후 다시 시도해 주세요"


def normalize_nickname(value: str) -> str:
    """앞뒤 공백을 지우고 규칙에 맞는지 본다. 중복은 허용한다(표시 이름일 뿐 식별자가 아니다)."""
    name = value.strip()
    if not NICKNAME_MIN <= len(name) <= NICKNAME_MAX:
        raise validation_error("nickname", f"닉네임은 {NICKNAME_MIN}~{NICKNAME_MAX}자로 정해 주세요")
    if not _NICKNAME.fullmatch(name):
        raise validation_error("nickname", "한글·영문·숫자만 쓸 수 있어요(띄어쓰기·기호 없이)")
    return name


@dataclass(frozen=True)
class Session:
    account: Account
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class SignupNeeded:
    signup_token: str
    profile_nickname: str | None


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str


class AuthService:
    def __init__(
        self,
        repo: AccountRepository | None,
        providers: Mapping[SocialProvider, OAuthProvider],
        signer: JwtSigner | None,
        redirect_uris: list[str],
        clock: Clock,
    ):
        self._repo = repo
        self._providers = providers
        self._signer = signer
        self._redirect_uris = frozenset(redirect_uris)
        self._clock = clock

    def _ready(self) -> tuple[AccountRepository, JwtSigner]:
        """서명 키(PUGURIN_JWT_SECRET)나 DB가 없으면 인증 API 전체가 503이다."""
        if self._repo is None or self._signer is None:
            raise source_unavailable(NOT_CONFIGURED)
        return self._repo, self._signer

    def _provider(self, provider: SocialProvider) -> OAuthProvider:
        found = self._providers.get(provider)
        if found is None:
            raise source_unavailable(f"{provider.value} 로그인을 아직 쓸 수 없어요")
        return found

    # ── 1. 로그인 ──

    async def login_with_token(self, provider: SocialProvider, token: str, device_id: UUID) -> Session | SignupNeeded:
        """앱: 소셜 SDK 토큰을 제공자 API로 확인한다(우리 앱에 발급된 토큰만)."""
        self._ready()
        oauth = self._provider(provider)
        if provider not in APP_TOKEN_PROVIDERS:
            raise validation_error("access_token", APP_TOKEN_REFUSED)
        return await self._login(await self._social(oauth.verify_token(token)), device_id)

    async def login_with_code(
        self,
        provider: SocialProvider,
        code: str,
        redirect_uri: str,
        state: str,
        code_verifier: str | None,
        device_id: UUID,
    ) -> Session | SignupNeeded:
        """웹: 허용된 redirect_uri인지 먼저 본 뒤 인가 코드를 토큰으로 바꾼다."""
        self._ready()
        oauth = self._provider(provider)
        if redirect_uri not in self._redirect_uris:
            raise validation_error("redirect_uri", "허용되지 않은 redirect_uri입니다")
        return await self._login(
            await self._social(oauth.exchange_code(code, redirect_uri, state, code_verifier)), device_id
        )

    @staticmethod
    async def _social(pending) -> SocialProfile:
        try:
            return await pending
        except AppTokenNotSupported:
            raise validation_error("access_token", APP_TOKEN_REFUSED) from None
        except SocialAuthRejected:
            raise unauthorized("소셜 로그인을 확인하지 못했어요. 다시 시도해 주세요") from None
        except SourceUnavailable as e:
            logger.warning("소셜 로그인 확인 실패: %s", e)
            raise source_unavailable("소셜 로그인 서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요") from None

    async def _login(self, profile: SocialProfile, device_id: UUID) -> Session | SignupNeeded:
        repo, _ = self._ready()
        account = await repo.find_by_identity(profile.provider, profile.provider_user_id)
        if account is not None:
            return await self._start_session(account, device_id)
        now = self._clock()
        token = new_opaque_token()
        claim = SignupClaim(profile.provider, profile.provider_user_id, profile.nickname)
        await repo.save_signup_token(hash_token(token), claim, expires_at=now + SIGNUP_TTL, now=now)
        return SignupNeeded(token, profile.nickname)

    # ── 2. 가입 마무리 ──

    async def signup(
        self, signup_token: str, nickname: str, agreements: Mapping[str, bool], device_id: UUID
    ) -> Session:
        repo, _ = self._ready()
        # 요청을 다 확인한 뒤에 가입 토큰을 쓴다(동의 누락으로 토큰을 잃지 않게)
        for name in REQUIRED_AGREEMENTS:
            if not agreements.get(name):
                raise validation_error(name, "필수 항목에 동의해 주세요")
        name = normalize_nickname(nickname)
        now = self._clock()
        claim = await repo.take_signup_token(hash_token(signup_token), now=now)
        if claim is None:
            raise unauthorized("가입 시간이 지났어요. 다시 로그인해 주세요")
        account = await repo.create(claim, name, marketing=bool(agreements.get("marketing")), now=now)
        return await self._start_session(account, device_id)

    # ── 4. 토큰 ──

    async def _start_session(self, account: Account, device_id: UUID) -> Session:
        repo, signer = self._ready()
        now = self._clock()
        refresh = new_opaque_token()
        await repo.add_refresh_token(
            account.id, uuid4(), hash_token(refresh), device_id, now=now, expires_at=now + REFRESH_TTL
        )
        return Session(account, signer.issue(account.id, account.role, now=now), refresh)

    async def refresh(self, refresh_token: str, device_id: UUID) -> Tokens:
        repo, signer = self._ready()
        now = self._clock()
        new = new_opaque_token()
        outcome, user_id = await repo.rotate_refresh_token(
            hash_token(refresh_token), hash_token(new), device_id, now=now, expires_at=now + REFRESH_TTL
        )
        if outcome is RotateOutcome.reused:
            logger.warning("폐기된 refresh token 재사용 감지: 사용자 %s의 세션을 모두 끊었습니다", user_id)
        account = await repo.get(user_id) if outcome is RotateOutcome.rotated and user_id else None
        if account is None:
            raise unauthorized()
        return Tokens(signer.issue(account.id, account.role, now=now), new)

    async def authenticate(self, authorization: str | None) -> Account:
        """`Authorization: Bearer <access token>` → 사용자. 없거나 틀리거나 만료됐거나 탈퇴했으면 401."""
        repo, signer = self._ready()
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise unauthorized()
        try:
            claims = signer.verify(token.strip(), now=self._clock())
        except InvalidToken:
            raise unauthorized() from None
        account = await repo.get(claims.user_id)
        if account is None:
            raise unauthorized()
        return account

    # ── 5. 로그아웃 ──

    async def logout(self, refresh_token: str | None) -> None:
        """그 refresh token만 폐기한다. 모르는 토큰이어도 성공으로 본다(이미 로그아웃된 상태)."""
        repo, _ = self._ready()
        if refresh_token:
            await repo.revoke_refresh_token(hash_token(refresh_token), now=self._clock())

    # ── 6·7. 내 정보·탈퇴 ──

    async def update_me(self, account: Account, nickname: str | None, settings: Mapping[str, bool] | None) -> Account:
        repo, _ = self._ready()
        name = normalize_nickname(nickname) if nickname is not None else None
        updated = await repo.update(account.id, nickname=name, settings=dict(settings) if settings else None)
        if updated is None:
            raise unauthorized()
        return updated

    async def withdraw(self, account: Account) -> None:
        repo, _ = self._ready()
        await repo.withdraw(account.id, now=self._clock())

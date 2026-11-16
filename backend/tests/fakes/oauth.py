"""테스트용 소셜 로그인 제공자. 미리 넣어 둔 토큰·인가 코드만 받아 준다(키·네트워크 없이)."""

from dataclasses import dataclass, field

from app.adapters.oauth import SocialAuthRejected
from app.repositories.ports import SourceUnavailable
from app.repositories.types import SocialProfile, SocialProvider


@dataclass
class FakeOAuth:
    provider: SocialProvider
    tokens: dict[str, SocialProfile] = field(default_factory=dict)
    codes: dict[str, SocialProfile] = field(default_factory=dict)
    exchanges: list[tuple[str, str, str, str | None]] = field(default_factory=list)
    down: bool = False

    def user(self, user_id: str, nickname: str | None = None) -> SocialProfile:
        return SocialProfile(self.provider, user_id, nickname)

    async def verify_token(self, token: str) -> SocialProfile:
        if self.down:
            raise SourceUnavailable("제공자 장애")
        if token not in self.tokens:
            raise SocialAuthRejected("모르는 토큰")
        return self.tokens[token]

    async def exchange_code(self, code: str, redirect_uri: str, state: str, code_verifier: str | None) -> SocialProfile:
        self.exchanges.append((code, redirect_uri, state, code_verifier))
        if self.down:
            raise SourceUnavailable("제공자 장애")
        # 인가 코드는 1회용이다
        profile = self.codes.pop(code, None)
        if profile is None:
            raise SocialAuthRejected("모르는 코드")
        return profile

    async def aclose(self) -> None:
        return None

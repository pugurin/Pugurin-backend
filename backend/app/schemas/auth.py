from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.dates import KST
from app.repositories.types import Account, SocialProvider

DEFAULT_SETTINGS = {"loading_tips": True, "unseen_term_dots": True}
MARKETING_RECONFIRM_YEARS = 2  # 광고성 정보 수신동의는 2년마다 다시 받는다(정보통신망법)


# ── 요청 ──


class OAuthLoginIn(BaseModel):
    """앱은 access_token, 웹(X-Client: web)은 code·redirect_uri·state(+code_verifier)."""

    access_token: str | None = Field(None, min_length=1, max_length=4096, description="앱: 소셜 SDK가 준 토큰")
    code: str | None = Field(None, min_length=1, max_length=2048, description="웹: 인가 코드")
    redirect_uri: str | None = Field(
        None, min_length=1, max_length=2048, description="웹: 허용 목록과 정확히 같아야 함"
    )
    state: str | None = Field(None, min_length=1, max_length=1024, description="웹: 웹이 만들어 콜백에서 확인한 값")
    code_verifier: str | None = Field(None, min_length=43, max_length=128, description="웹: PKCE(지원하는 제공자만)")


class Agreements(BaseModel):
    terms: bool
    privacy: bool
    age14: bool
    marketing: bool = False


class SignupIn(BaseModel):
    signup_token: str = Field(min_length=1, max_length=256)
    nickname: str = Field(max_length=64)
    agreements: Agreements


class RefreshIn(BaseModel):
    refresh_token: str | None = Field(None, min_length=1, max_length=256)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loading_tips: bool | None = None
    unseen_term_dots: bool | None = None


class UserPatch(BaseModel):
    nickname: str | None = Field(None, max_length=64)
    settings: SettingsPatch | None = None


# ── 응답 ──


class ConsentsOut(BaseModel):
    """알림 동의 변경(PUT /users/me/consents)은 다음 PR. 지금은 마케팅 동의 말고는 기본값이다."""

    chat_push: bool = True
    interest_alert_push: bool = True
    study_push: bool = False
    study_push_time: str = "20:00"
    marketing_push: bool
    marketing_night_push: bool = False
    marketing_consented_at: datetime | None
    marketing_reconfirm_due: datetime | None


class UserSettingsOut(BaseModel):
    loading_tips: bool
    unseen_term_dots: bool


class UserOut(BaseModel):
    id: str
    nickname: str
    role: Literal["buyer", "admin"]
    provider: SocialProvider
    verified_ownership_count: int = 0  # 소유 확인(3차 기능)이 생기기 전까지 0
    active_listing_count: int = 0
    consents: ConsentsOut
    settings: UserSettingsOut
    created_at: datetime

    @classmethod
    def of(cls, account: Account) -> "UserOut":
        consented = account.marketing_consented_at.astimezone(KST) if account.marketing_consented_at else None
        return cls(
            id=str(account.id),
            nickname=account.nickname,
            role=account.role,
            provider=account.provider,
            consents=ConsentsOut(
                marketing_push=account.marketing_push,
                marketing_consented_at=consented,
                marketing_reconfirm_due=_add_years(consented, MARKETING_RECONFIRM_YEARS) if consented else None,
            ),
            settings=UserSettingsOut(**{**DEFAULT_SETTINGS, **account.settings}),
            created_at=account.created_at.astimezone(KST),
        )


def _add_years(d: datetime, years: int) -> datetime:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2월 29일
        return d.replace(year=d.year + years, day=28)


class SessionOut(BaseModel):
    """가입한 사람의 로그인·가입 마무리 응답. 웹은 refresh_token이 본문에 없고 쿠키로만 간다."""

    is_new_user: Literal[False] = False
    access_token: str
    refresh_token: str | None = Field(None, description="앱만. 웹은 HttpOnly 쿠키")
    user: UserOut


class SignupProfileOut(BaseModel):
    nickname: str | None = Field(description="소셜 프로필 이름. 없으면 null")


class SignupNeededOut(BaseModel):
    """처음 온 사람: 토큰 대신 가입 토큰(10분, 1회용)을 준다. POST /auth/signup으로 가입을 마친다."""

    is_new_user: Literal[True] = True
    signup_token: str
    profile: SignupProfileOut


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str | None = Field(None, description="앱만. 웹은 HttpOnly 쿠키")

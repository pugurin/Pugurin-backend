"""로그인·가입·토큰 갱신·로그아웃 (명세 §8, docs/AUTH.md).

웹(X-Client: web)은 refresh token을 본문에 넣지 않고 HttpOnly 쿠키로만 주고받는다. 쿠키는 X-Client: web일 때만 읽는다
(사용자 지정 헤더라 브라우저가 사전 요청을 보내므로 다른 사이트가 쿠키로 갱신·로그아웃을 시킬 수 없다).
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth.tokens import REFRESH_TTL
from app.core.errors import AppError, ErrorCode, error_body, unauthorized, validation_error
from app.repositories.types import SocialProvider
from app.routers.deps import ClientDep, ClientKind, ContainerDep, DeviceIdDep
from app.schemas.auth import (
    OAuthLoginIn,
    RefreshIn,
    SessionOut,
    SignupIn,
    SignupNeededOut,
    SignupProfileOut,
    TokensOut,
    UserOut,
)
from app.schemas.common import PlainEnvelope
from app.services.auth_service import ConcurrentRefresh, Session, SignupNeeded

router = APIRouter(tags=["auth"])

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api/v1/auth"  # 갱신·로그아웃 요청에만 실린다


def _json(data: BaseModel, *, exclude: set[str] | None = None) -> JSONResponse:
    # 토큰이 담긴 응답은 어디에도 캐시되면 안 된다
    return JSONResponse(
        {"data": data.model_dump(mode="json", exclude=exclude), "meta": {}}, headers={"Cache-Control": "no-store"}
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=int(REFRESH_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="lax")


def _session_response(result: Session | SignupNeeded, client: ClientKind) -> JSONResponse:
    if isinstance(result, SignupNeeded):
        return _json(
            SignupNeededOut(
                signup_token=result.signup_token, profile=SignupProfileOut(nickname=result.profile_nickname)
            )
        )
    web = client is ClientKind.web
    body = SessionOut(
        access_token=result.access_token,
        refresh_token=None if web else result.refresh_token,
        user=UserOut.of(result.account),
    )
    response = _json(body, exclude={"refresh_token"} if web else None)
    if web:
        set_refresh_cookie(response, result.refresh_token)
    return response


@router.post(
    "/auth/oauth/{provider}",
    response_model=PlainEnvelope[SessionOut | SignupNeededOut],
    summary="소셜 로그인. 처음 온 사람은 가입 토큰을 받는다",
)
async def oauth_login(
    provider: SocialProvider, body: OAuthLoginIn, container: ContainerDep, client: ClientDep, device_id: DeviceIdDep
) -> JSONResponse:
    auth = container.auth_service
    if client is ClientKind.web:
        for field in ("code", "redirect_uri", "state"):
            if not getattr(body, field):
                raise validation_error(field, f"웹 로그인에는 {field}가 필요합니다")
        result = await auth.login_with_code(
            provider, body.code, body.redirect_uri, body.state, body.code_verifier, device_id
        )
    else:
        if not body.access_token:
            raise validation_error("access_token", "소셜 SDK가 준 access_token이 필요합니다")
        result = await auth.login_with_token(provider, body.access_token, device_id)
    return _session_response(result, client)


@router.post("/auth/signup", response_model=PlainEnvelope[SessionOut], summary="가입 마무리(닉네임·약관 동의)")
async def signup(body: SignupIn, container: ContainerDep, client: ClientDep, device_id: DeviceIdDep) -> JSONResponse:
    session = await container.auth_service.signup(
        body.signup_token, body.nickname, body.agreements.model_dump(), device_id
    )
    return _session_response(session, client)


@router.post("/auth/refresh", response_model=PlainEnvelope[TokensOut], summary="access token 재발급 + refresh rotation")
async def refresh(
    container: ContainerDep,
    client: ClientDep,
    device_id: DeviceIdDep,
    body: RefreshIn | None = None,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> JSONResponse:
    if client is ClientKind.web:
        # 웹은 쿠키만 본다(본문의 토큰은 받지 않는다). 401이면(없음·만료·재사용) 쓸모없어진 쿠키도 지운다
        try:
            if not refresh_token:
                raise unauthorized()
            tokens = await container.auth_service.refresh(refresh_token, device_id)
        except AppError as e:
            # 동시 갱신(유예)으로 진 쪽은 쿠키를 지우지 않는다: 먼저 끝난 갱신이 내려준 새 쿠키를 지우게 된다
            if e.code is not ErrorCode.UNAUTHORIZED or isinstance(e, ConcurrentRefresh):
                raise
            failed = JSONResponse(error_body(e.code, e.message, e.field), status_code=e.status_code)
            clear_refresh_cookie(failed)
            return failed
        response = _json(TokensOut(access_token=tokens.access_token), exclude={"refresh_token"})
        set_refresh_cookie(response, tokens.refresh_token)
        return response
    if body is None or not body.refresh_token:
        raise validation_error("refresh_token", "refresh_token이 필요합니다")
    tokens = await container.auth_service.refresh(body.refresh_token, device_id)
    return _json(TokensOut(access_token=tokens.access_token, refresh_token=tokens.refresh_token))


@router.post("/auth/logout", status_code=204, summary="이 refresh token 폐기(웹은 쿠키도 지운다)")
async def logout(
    container: ContainerDep,
    client: ClientDep,
    body: RefreshIn | None = None,
    refresh_token: Annotated[str | None, Cookie(include_in_schema=False)] = None,
) -> Response:
    web = client is ClientKind.web
    await container.auth_service.logout(refresh_token if web else (body.refresh_token if body else None))
    response = Response(status_code=204)
    if web:
        clear_refresh_cookie(response)
    return response

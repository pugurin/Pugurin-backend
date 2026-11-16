"""내 정보·닉네임·설정 수정·회원 탈퇴 (명세 §8, docs/AUTH.md). 모두 access token(Bearer)이 필요하다."""

from fastapi import APIRouter, Response

from app.routers.auth import clear_refresh_cookie
from app.routers.deps import ClientDep, ClientKind, ContainerDep, CurrentUser
from app.schemas.auth import UserOut, UserPatch
from app.schemas.common import PlainEnvelope

router = APIRouter(tags=["users"])

NO_STORE = {"Cache-Control": "no-store"}


@router.get("/users/me", response_model=PlainEnvelope[UserOut], summary="내 정보")
async def get_me(user: CurrentUser, response: Response) -> PlainEnvelope[UserOut]:
    response.headers.update(NO_STORE)
    return PlainEnvelope(data=UserOut.of(user))


@router.patch("/users/me", response_model=PlainEnvelope[UserOut], summary="닉네임·설정 수정")
async def patch_me(body: UserPatch, user: CurrentUser, container: ContainerDep, response: Response):
    settings = body.settings.model_dump(exclude_none=True) if body.settings else None
    updated = await container.auth_service.update_me(user, body.nickname, settings)
    response.headers.update(NO_STORE)
    return PlainEnvelope(data=UserOut.of(updated))


@router.delete("/users/me", status_code=204, summary="회원 탈퇴(개인정보 즉시 익명화, 모든 세션 종료)")
async def delete_me(user: CurrentUser, container: ContainerDep, client: ClientDep) -> Response:
    await container.auth_service.withdraw(user)
    response = Response(status_code=204)
    if client is ClientKind.web:
        clear_refresh_cookie(response)
    return response

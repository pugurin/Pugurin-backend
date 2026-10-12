import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.errors import not_found
from app.routers.deps import ContainerDep

router = APIRouter(tags=["internal"], include_in_schema=False)


def _is_internal(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


@router.get("/internal/health/ready")
async def ready(request: Request, container: ContainerDep) -> JSONResponse:
    """내부망 전용 readiness. 외부에서 부르면 없는 경로처럼 404를 준다."""
    if not _is_internal(request):
        raise not_found("요청한 경로를 찾을 수 없습니다")
    result = await container.readiness_service.check()
    body = {
        "status": "ready" if result.ready else "not_ready",
        "data_mode": container.data_mode,
        "checks": {name: {"status": c.status, "detail": c.detail} for name, c in result.checks.items()},
    }
    return JSONResponse(body, status_code=200 if result.ready else 503, headers={"Cache-Control": "no-store"})

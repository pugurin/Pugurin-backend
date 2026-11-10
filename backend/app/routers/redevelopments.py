import math
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.errors import validation_error
from app.core.geo import BBox
from app.repositories.types import RedevelopmentFilter, RedevelopmentKind, RedevelopmentStage
from app.routers.deps import ContainerDep
from app.schemas.redevelopment import (
    RedevelopmentMeta,
    RedevelopmentOut,
    RedevelopmentPageMeta,
    RedevelopmentResponse,
    RedevelopmentsResponse,
)

router = APIRouter(tags=["redevelopments"])


def _parse[E: StrEnum](raw: str | None, enum: type[E], field: str) -> tuple[E, ...]:
    if not raw:
        return ()
    try:
        return tuple(enum(v.strip()) for v in raw.split(",") if v.strip())
    except ValueError:
        allowed = ",".join(e.value for e in enum)
        raise validation_error(field, f"{field}은 {allowed} 중에서 고르세요") from None


@router.get(
    "/redevelopments", response_model=RedevelopmentsResponse, summary="정비사업(재개발·재건축) 구역 목록 (지도·목록)"
)
async def list_redevelopments(
    container: ContainerDep,
    bbox: Annotated[
        str | None, Query(description="min_lng,min_lat,max_lng,max_lat. 지도 화면 안의 구역(좌표가 있는 구역만)")
    ] = None,
    kind: Annotated[
        str | None,
        Query(
            description="쉼표 구분: redevelopment,reconstruction,small_reconstruction,small_redevelopment,"
            "street_housing,residential_improvement,urban_environment,other. 생략하면 전부"
        ),
    ] = None,
    stage: Annotated[
        str | None,
        Query(
            description="쉼표 구분: cancelled,planned,designated,committee,association,review,project_approved,"
            "disposal_approved,construction,completed,transferred,dissolved,unknown. 생략하면 전부"
        ),
    ] = None,
    include_closed: Annotated[bool, Query(description="원천 목록에서 빠진 구역도 포함")] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> RedevelopmentsResponse:
    flt = RedevelopmentFilter(
        bbox=BBox.parse(bbox) if bbox else None,
        kinds=_parse(kind, RedevelopmentKind, "kind"),
        stages=_parse(stage, RedevelopmentStage, "stage"),
        include_closed=include_closed,
    )
    result, as_of = await container.redevelopment_service.search(flt, page=page, page_size=page_size)
    return RedevelopmentsResponse(
        data=[RedevelopmentOut.from_domain(z) for z in result.items],
        meta=RedevelopmentPageMeta(
            data_as_of=as_of,
            page=page,
            page_size=page_size,
            total=result.total,
            total_pages=math.ceil(result.total / page_size),
        ),
    )


@router.get("/redevelopments/{redevelopment_id}", response_model=RedevelopmentResponse, summary="정비사업 구역 상세")
async def get_redevelopment(redevelopment_id: str, container: ContainerDep) -> RedevelopmentResponse:
    found, as_of = await container.redevelopment_service.get(redevelopment_id)
    return RedevelopmentResponse(data=RedevelopmentOut.from_domain(found), meta=RedevelopmentMeta(data_as_of=as_of))

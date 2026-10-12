from typing import Annotated

from fastapi import APIRouter, Query

from app.core.geo import BBox
from app.routers.deps import ContainerDep
from app.routers.filters import MarketFilter
from app.schemas.common import DataMeta
from app.schemas.map import ComplexMarker, MarkersData, MarkersResponse, ParcelMarker, RegionMarker

router = APIRouter(tags=["map"])


@router.get("/map/markers", response_model=MarkersResponse, summary="지도 마커·집계 조회")
async def get_markers(
    container: ContainerDep,
    flt: MarketFilter,
    bbox: Annotated[str, Query(description="min_lng,min_lat,max_lng,max_lat")],
    zoom: Annotated[int, Query(ge=0, le=22, description="표준 웹 메르카토르 줌 (256px 타일 기준 정수)")],
) -> MarkersResponse:
    area = BBox.parse(bbox)
    result = await container.map_service.markers(area, zoom, flt)
    markers = [
        *(RegionMarker.from_domain(r, flt.deal_type) for r in result.regions),
        *(ComplexMarker.from_domain(c) for c in result.complexes),
        *(ParcelMarker.from_domain(p) for p in result.parcels),
    ]
    return MarkersResponse(
        data=MarkersData(level=result.level, markers=markers),
        meta=DataMeta(data_as_of=result.data_as_of),
    )

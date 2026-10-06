from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.core.geo import m2_to_pyeong
from app.routers.deps import ContainerDep
from app.schemas.parcel import ParcelMeta, ParcelOut, ParcelResponse, RestrictionOut, ZoningOut
from app.services.parcel_service import ParcelResult

router = APIRouter(tags=["parcels"])


def _to_response(result: ParcelResult, response: Response) -> ParcelResponse:
    if result.unavailable_reason == "source_unavailable":
        response.headers["Cache-Control"] = "no-store"
    meta = ParcelMeta(cached_at=result.cached_at, unavailable_reason=result.unavailable_reason)
    view = result.parcel
    if view is None:
        return ParcelResponse(data=None, meta=meta)

    d = view.data
    return ParcelResponse(
        data=ParcelOut(
            pnu=d.pnu,
            jibun_address=d.jibun_address,
            land_category=d.land_category,
            land_area_m2=d.land_area_m2,
            land_area_pyeong=m2_to_pyeong(d.land_area_m2),
            official_land_price_per_m2=d.official_land_price_per_m2,
            official_land_price_year=d.official_land_price_year,
            road_side=d.road_side,
            shape=d.shape,
            geometry=d.geometry,
            zonings=[
                ZoningOut(
                    zone_type=z.part.zone_type,
                    area_ratio=z.part.area_ratio,
                    inclusion=z.part.inclusion,
                    max_building_coverage_ratio=z.limits.max_building_coverage_ratio if z.limits else None,
                    max_floor_area_ratio=z.limits.max_floor_area_ratio if z.limits else None,
                )
                for z in view.zonings
            ],
            ratio_source=view.ratio_source,
            restrictions=[
                RestrictionOut(name=r.name, plain_explanation=r.plain_explanation, glossary_term_id=term_id)
                for r, term_id in zip(d.restrictions, view.restriction_term_ids, strict=True)
            ],
            glossary=view.glossary,
        ),
        meta=meta,
    )


@router.get("/parcels/lookup", response_model=ParcelResponse, summary="좌표로 필지 조회")
async def lookup_parcel(
    container: ContainerDep,
    response: Response,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
) -> ParcelResponse:
    return _to_response(await container.parcel_service.lookup(lat, lng), response)


@router.get("/parcels/{pnu}", response_model=ParcelResponse, summary="PNU로 필지 조회")
async def get_parcel(
    container: ContainerDep,
    response: Response,
    pnu: Annotated[str, Path(pattern=r"^\d{19}$", description="필지고유번호 19자리")],
) -> ParcelResponse:
    return _to_response(await container.parcel_service.by_pnu(pnu), response)

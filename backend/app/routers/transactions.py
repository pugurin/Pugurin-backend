import math
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Path, Query

from app.core.errors import validation_error
from app.core.geo import BBox
from app.routers.deps import ContainerDep
from app.routers.filters import MarketFilter
from app.schemas.common import DataMeta, PageMeta
from app.schemas.complex import TransactionOut, TransactionResponse, TransactionsResponse

router = APIRouter(tags=["transactions"])


@router.get("/transactions", response_model=TransactionsResponse, summary="거래 목록")
async def list_transactions(
    container: ContainerDep,
    flt: MarketFilter,
    bbox: Annotated[str | None, Query(description="min_lng,min_lat,max_lng,max_lat")] = None,
    region_code: Annotated[
        str | None, Query(pattern=r"^(\d{5}|\d{10})$", description="시군구 5자리 또는 법정동 10자리")
    ] = None,
    include_cancelled: bool = False,
    sort: Literal["contract_date_desc", "price_asc", "price_desc"] = "contract_date_desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TransactionsResponse:
    if bbox is None and region_code is None:
        raise validation_error("bbox", "bbox 또는 region_code 중 하나는 필요합니다")
    result = await container.transaction_service.list(
        flt,
        bbox=BBox.parse(bbox) if bbox else None,
        region_code=region_code,
        include_cancelled=include_cancelled,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return TransactionsResponse(
        data=[TransactionOut.from_domain(tx) for tx in result.items],
        meta=PageMeta(
            data_as_of=container.transaction_service.data_as_of,
            page=page,
            page_size=page_size,
            total=result.total,
            total_pages=math.ceil(result.total / page_size),
        ),
    )


@router.get("/transactions/{transaction_id}", response_model=TransactionResponse, summary="거래 단건")
async def get_transaction(
    container: ContainerDep, transaction_id: Annotated[UUID, Path(description="거래 ID")]
) -> TransactionResponse:
    found = await container.transaction_service.get(transaction_id)
    return TransactionResponse(
        data=TransactionOut.from_domain(found), meta=DataMeta(data_as_of=container.transaction_service.data_as_of)
    )

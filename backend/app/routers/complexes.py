import math
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from app.repositories.types import DealType
from app.routers.deps import ContainerDep
from app.schemas.common import PageMeta
from app.schemas.complex import ComplexOut, ComplexResponse, TransactionOut, TransactionsResponse

router = APIRouter(tags=["complexes"])


@router.get("/complexes/{complex_id}", response_model=ComplexResponse, summary="단지·건물 정보")
async def get_complex(complex_id: UUID, container: ContainerDep) -> ComplexResponse:
    found = await container.complex_service.get(complex_id)
    return ComplexResponse(data=ComplexOut.from_domain(found))


@router.get("/complexes/{complex_id}/transactions", response_model=TransactionsResponse, summary="단지 거래 이력")
async def list_complex_transactions(
    complex_id: UUID,
    container: ContainerDep,
    deal_type: DealType | None = None,
    exclude_direct: bool = False,
    include_cancelled: bool = False,
    sort: Literal["contract_date_desc", "price_asc", "price_desc"] = "contract_date_desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TransactionsResponse:
    result = await container.complex_service.transactions(
        complex_id,
        deal_type=deal_type,
        exclude_direct=exclude_direct,
        include_cancelled=include_cancelled,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return TransactionsResponse(
        data=[TransactionOut.from_domain(tx) for tx in result.items],
        meta=PageMeta(
            data_as_of=container.complex_service.data_as_of,
            page=page,
            page_size=page_size,
            total=result.total,
            total_pages=math.ceil(result.total / page_size),
        ),
    )

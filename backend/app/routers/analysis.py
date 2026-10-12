from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.repositories.types import DealType
from app.routers.deps import ContainerDep
from app.schemas.analysis import AnalysisMeta, ComplexAnalysisResponse, complex_analysis_out
from app.schemas.stats import stats_method

router = APIRouter(tags=["analysis"])


@router.get(
    "/analysis/complexes/{complex_id}",
    response_model=ComplexAnalysisResponse,
    summary="단지 분석: 단지 vs 같은 법정동 시세 추이 + 용도지역",
)
async def analyze_complex(
    container: ContainerDep,
    complex_id: UUID,
    deal_type: DealType = DealType.sale,
    period_months: Annotated[int, Query(ge=1, le=60)] = 36,
    exclude_direct: bool = False,
) -> ComplexAnalysisResponse:
    result = await container.analysis_service.complex(complex_id, deal_type, period_months, exclude_direct)
    return ComplexAnalysisResponse(
        data=complex_analysis_out(deal_type, result),
        meta=AnalysisMeta(
            data_as_of=container.complex_service.data_as_of,
            method=stats_method(exclude_direct),
            zoning_unavailable_reason=result.zoning_unavailable_reason,
        ),
    )

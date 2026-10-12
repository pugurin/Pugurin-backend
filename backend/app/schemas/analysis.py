from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.repositories.types import DealType, PropertyType
from app.schemas.parcel import ZoningOut, zoning_out
from app.schemas.stats import RegionStatsOut, StatsMeta, series_out
from app.services.analysis_service import ComplexAnalysis

ZoningUnavailableReason = Literal["no_pnu", "no_parcel", "out_of_service_area", "source_unavailable"]


class AnalyzedComplexOut(BaseModel):
    id: UUID
    name: str
    property_type: PropertyType
    region_code: str


class ComparedRegionOut(BaseModel):
    region_code: str
    name: str


class ComparisonOut(BaseModel):
    metric: str
    complex_value: int
    region_value: int
    difference_percent: float
    summary: str


class ComplexAnalysisOut(BaseModel):
    complex: AnalyzedComplexOut
    region: ComparedRegionOut
    complex_stats: RegionStatsOut
    region_stats: RegionStatsOut
    comparison: ComparisonOut | None
    zonings: list[ZoningOut] | None
    disclaimer: str


class AnalysisMeta(StatsMeta):
    zoning_unavailable_reason: ZoningUnavailableReason | None


class ComplexAnalysisResponse(BaseModel):
    data: ComplexAnalysisOut
    meta: AnalysisMeta


def complex_analysis_out(deal_type: DealType, a: ComplexAnalysis) -> ComplexAnalysisOut:
    c = a.comparison
    return ComplexAnalysisOut(
        complex=AnalyzedComplexOut(
            id=a.complex.id, name=a.complex.name, property_type=a.complex.property_type, region_code=a.region_code
        ),
        region=ComparedRegionOut(region_code=a.region_code, name=a.region_name),
        complex_stats=series_out(deal_type, a.complex_stats.overall),
        region_stats=series_out(deal_type, a.region_stats.overall),
        comparison=ComparisonOut(
            metric=c.metric,
            complex_value=c.complex_value,
            region_value=c.region_value,
            difference_percent=c.difference_percent,
            summary=c.summary,
        )
        if c
        else None,
        zonings=[zoning_out(z) for z in a.zonings] if a.zonings is not None else None,
        disclaimer=a.disclaimer,
    )

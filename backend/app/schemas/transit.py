from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.adapters.odsay import Leg, Route
from app.services.transit_service import TransitResult, UnavailableReason


class LineOut(BaseModel):
    name: str
    color: str | None  # #RRGGBB, 모르면 null


class LegOut(BaseModel):
    mode: Literal["walk", "subway", "bus", "etc"]
    minutes: int
    distance_m: int
    line: LineOut | None
    start_name: str | None
    end_name: str | None
    stop_count: int | None
    path: list[list[float]] | None  # [[lng, lat], ...]. 못 구하면 null

    @classmethod
    def from_domain(cls, leg: Leg) -> "LegOut":
        return cls(
            mode=leg.mode,  # type: ignore[arg-type]
            minutes=leg.minutes,
            distance_m=leg.distance_m,
            line=LineOut(name=leg.line.name, color=leg.line.color) if leg.line else None,
            start_name=leg.start_name,
            end_name=leg.end_name,
            stop_count=leg.stop_count,
            path=leg.path,
        )


class RouteOut(BaseModel):
    total_minutes: int
    transfer_count: int
    fare: int | None
    walk_minutes: int
    distance_m: int
    legs: list[LegOut]

    @classmethod
    def from_domain(cls, r: Route) -> "RouteOut":
        return cls(
            total_minutes=r.total_minutes,
            transfer_count=r.transfer_count,
            fare=r.fare,
            walk_minutes=r.walk_minutes,
            distance_m=r.distance_m,
            legs=[LegOut.from_domain(leg) for leg in r.legs],
        )


class TransitRoutesOut(BaseModel):
    routes: list[RouteOut]
    unavailable_reason: UnavailableReason | None  # too_close·no_route·source_unavailable. 경로가 있으면 null


class TransitMeta(BaseModel):
    data_as_of: datetime  # 이 결과를 ODsay에서 받은 시각(24시간 캐시)


class TransitRoutesResponse(BaseModel):
    data: TransitRoutesOut
    meta: TransitMeta

    @classmethod
    def from_result(cls, result: TransitResult) -> "TransitRoutesResponse":
        return cls(
            data=TransitRoutesOut(
                routes=[RouteOut.from_domain(r) for r in result.routes],
                unavailable_reason=result.unavailable_reason,
            ),
            meta=TransitMeta(data_as_of=result.data_as_of),
        )

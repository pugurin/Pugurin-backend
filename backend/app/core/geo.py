import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.errors import validation_error

SQM_PER_PYEONG = 3.3058


def m2_to_pyeong(m2: float) -> float:
    return round(m2 / SQM_PER_PYEONG, 1)


def pyeong_exact(m2: float) -> float:
    return m2 / SQM_PER_PYEONG


def round_area(m2: float) -> float:
    """평형 대표 면적(중위값)을 소수 2자리로. 부동소수 오차를 6자리에서 정리한 뒤 사사오입한다(DB 적재와 같은 규칙)."""
    return float(Decimal(f"{m2:.6f}").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def area_bucket(m2: float) -> int:
    """평형 묶음: 전용면적을 반올림한 정수(㎡). 84.842와 84.97은 85다. DB의 floor(x + 0.5)와 같은 규칙(사사오입)."""
    return math.floor(m2 + 0.5)


@dataclass(frozen=True)
class BBox:
    min_lng: float
    min_lat: float
    max_lng: float
    max_lat: float

    def contains(self, lat: float, lng: float) -> bool:
        return self.min_lng <= lng <= self.max_lng and self.min_lat <= lat <= self.max_lat

    @classmethod
    def parse(cls, raw: str, field: str = "bbox") -> "BBox":
        try:
            parts = [float(p) for p in raw.split(",")]
        except ValueError:
            raise validation_error(field, "bbox는 'min_lng,min_lat,max_lng,max_lat' 숫자 4개여야 합니다") from None
        if len(parts) != 4:
            raise validation_error(field, "bbox는 'min_lng,min_lat,max_lng,max_lat' 숫자 4개여야 합니다")
        min_lng, min_lat, max_lng, max_lat = parts
        if not (-180 <= min_lng <= max_lng <= 180 and -90 <= min_lat <= max_lat <= 90):
            raise validation_error(field, "bbox 좌표 범위가 올바르지 않습니다")
        return cls(min_lng, min_lat, max_lng, max_lat)

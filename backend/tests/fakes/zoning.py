"""테스트용 용도지역별 건폐율·용적률 한도. 부산시 도시계획 조례 값이 아니다."""

from app.repositories.types import ZoningLimits

SAMPLE_ZONING_LIMITS: dict[str, tuple[int, int]] = {
    "제1종일반주거지역": (60, 150),
    "제2종일반주거지역": (60, 250),
    "제3종일반주거지역": (50, 300),
    "근린상업지역": (70, 600),
    "일반상업지역": (80, 1000),
    "자연녹지지역": (20, 100),
    "계획관리지역": (40, 100),
}

SOURCE_LABEL = "샘플 값 (부산시 도시계획 조례 확인 전)"


class SampleZoningRules:
    """테스트용 건폐율·용적률 표."""

    source_label = SOURCE_LABEL

    def limits(self, zone_type: str) -> ZoningLimits | None:
        found = SAMPLE_ZONING_LIMITS.get(zone_type)
        return ZoningLimits(*found) if found else None

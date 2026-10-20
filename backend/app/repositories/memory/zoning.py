from app.repositories.types import ZoningLimits


class NoZoningRules:
    """부산시 도시계획 조례의 용도지역별 건폐율·용적률 표를 확인하기 전까지는 한도를 내지 않는다(추정값 없음)."""

    source_label = "부산시 도시계획 조례 확인 전"

    def limits(self, zone_type: str) -> ZoningLimits | None:
        return None

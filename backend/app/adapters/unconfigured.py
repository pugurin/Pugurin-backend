from app.repositories.ports import SourceUnavailable
from app.repositories.types import ParcelData

MESSAGE = "VWorld 키가 설정되지 않아 토지 정보를 제공할 수 없습니다"


class UnconfiguredParcelSource:
    """VWorld 어댑터가 없는 실데이터 모드용. 가짜 필지 대신 '불러올 수 없음'으로 응답하게 한다."""

    async def resolve_pnu(self, lat: float, lng: float) -> str | None:
        raise SourceUnavailable(MESSAGE)

    async def fetch_parcel(self, pnu: str) -> ParcelData | None:
        raise SourceUnavailable(MESSAGE)

"""경매마당 API 가짜 응답. 이름은 모두 지어낸 것이고, 실제 응답처럼 개인정보 칸을 채워 둬서 걸러지는지 본다."""

from app.adapters.kakao import GeoResult
from app.adapters.madangs import AuctionItem, court_from_list, court_with_detail, public_from_list
from app.adapters.molit.types import QuotaExceeded

# 저장되면 안 되는 값들
PERSONAL = ("홍길동", "김세입", "박응찰", "이소유", "점유자메모")


def court_row(n: int, **over) -> dict:
    row = {
        "사건명칭": f"2025타경{1000 + n}",
        "법원명": "부산지방법원 동부지원",
        "product_id": f"CA-41-2025-{1000 + n}-1",
        "대표소재지명": f"부산광역시 해운대구 해운대로 {600 + n}, 9층90{n}호 (우동,가상마린)",
        "물건용도": "아파트",
        "유찰횟수": 1,
        "감정가": 500_000_000 + n,
        "최저가": 350_000_000 + n,
        "매각기일": "2026-11-10",
    }
    return {**row, **over}


def court_detail(row: dict) -> dict:
    n = int(row["product_id"].split("-")[3]) - 1000
    return {
        **row,
        "제공정보": {"키유형": "일반형", "제한항목": ["당사자내역[].당사자명"]},
        "사건명칭": "부동산임의경매",
        "지번주소": f"부산 해운대구 우동 {600 + n}",
        "토지면적": 20.5,
        "건물면적": 84.97,
        "보증금": 35_000_000,
        "청구액": 400_000_000,
        "당사자내역": [{"당사자구분": "소유자", "당사자명": "이소유"}, {"당사자구분": "채무자", "당사자명": "홍길동"}],
        "임차인현황": [{"임차인": "김세입", "보증금": 50_000_000, "전입일자": "2020-06-19"}],
        "현황조사": "김세입 세대가 전입되어 있음",
        "점유조사": "점유자메모: 홍길동 거주",
        "등기부현황": [{"등기일자": "2020-06-22", "권리종류": "소유자", "권리자": "이소유"}],
        "기일내역": [
            {"기일날짜": "2026-09-01", "기일종류": "매각기일", "최저매각가격": 500_000_000, "기일결과": "유찰"},
            {"기일날짜": "2026-11-10", "기일종류": "매각기일", "최저매각가격": 350_000_000, "기일결과": "진행"},
        ],
        "매각결과": [{"매각일자": "2026-09-01", "매각결과": "유찰", "응찰자수": 0, "응찰자명": "박응찰"}],
    }


def public_row(n: int, **over) -> dict:
    row = {
        "product_id": f"GO-2026-1000-{n:06d}",
        "공매관리번호": f"2026-1000-{n:06d}",
        "물건명": f"부산광역시 수영구 광안동 {100 + n} 가상빌라 제201호",
        "지번주소": f"부산 수영구 광안동 {100 + n}",
        "도로명주소": f"부산광역시 수영구 광안로 {100 + n}, 제201호",
        "물건상태": "입찰진행중",
        "물건용도": "다세대주택",
        "처분방식": "매각",
        "지분매각여부": False,
        "유찰횟수": 0,
        "감정가": 200_000_000,
        "최저입찰가": 180_000_000,
        "토지면적": 15.1,
        "건물면적": 49.8,
        "입찰시작일시": "2026-11-02 10:00",
        "입찰종료일시": "2026-11-04 17:00",
    }
    return {**row, **over}


class FakeAuctionSource:
    def __init__(self, court: list[dict], public: list[dict]):
        self.court, self.public = court, public
        self.detail_calls: list[str] = []
        self.calls_made = 0
        self.court_error: Exception | None = None
        self.quota_after: int | None = None  # 상세를 이만큼 받은 뒤 한도 초과

    async def court_list(self, region_prefix: str) -> list[AuctionItem]:
        self.calls_made += 1
        if self.court_error:
            raise self.court_error
        return [court_from_list(r) for r in self.court]

    async def court_detail(self, item: AuctionItem) -> AuctionItem:
        if self.quota_after is not None and len(self.detail_calls) >= self.quota_after:
            raise QuotaExceeded("경매마당 호출 한도 초과(HTTP 429)")
        self.detail_calls.append(item.id)
        self.calls_made += 1
        row = next(r for r in self.court if r["product_id"] == item.id)
        return court_with_detail(item, court_detail(row))

    async def public_list(self, region_prefix: str) -> list[AuctionItem]:
        self.calls_made += 1
        return [public_from_list(r) for r in self.public]


class FakeLookup:
    """주소 끝 번호로 결정적인 좌표를 만든다. '서울'이 들어간 주소는 부산 밖 결과를 돌려준다."""

    def __init__(self):
        self.queries: list[str] = []

    async def lookup(self, query: str) -> GeoResult | None:
        self.queries.append(query)
        if "서울" in query:
            return GeoResult(37.5, 127.0, "1111010100", "1111010100100010000", precise=True)
        digits = "".join(ch for ch in query.split()[-1] if ch.isdigit()) or "1"
        n = int(digits)
        b_code = "2635010500" if "해운대" in query else "2650010300"
        return GeoResult(35.16 + n / 100_000, 129.16 + n / 100_000, b_code, f"{b_code}1{n:04d}0000", precise=True)

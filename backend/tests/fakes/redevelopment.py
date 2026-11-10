"""부산 정비사업 정보 API 가짜 응답. 구역명·업체명·전화번호는 모두 지어낸 것이다.

실호출로 확인한 모양을 따른다: `response.header.resultCode`, `response.body.items.item`, 숫자도 문자열.
"""

from app.adapters.redevelopment import RedevelopmentItem, zone_from_row

IMG = "http://dynamice.busan.go.kr/data/busi_ara"


def zone_row(n: int, **over) -> dict:
    row = {
        "aCode": f"BARA_{n:013d}",
        "areaName": f"가상{n}구역 재개발",
        "location": f"부산진구 가야동 {100 + n}번지 일원(가상초교 남측 일원)",
        "areaUnit": "45,123.5",
        "step": "조합설립인가",
        "scale": "지하3층지상32층",
        "wingNum": "12",
        "generationJoo": "1,234",
        "gage": "299.9",
        "longPyeyul": "19.5",
        "guildMemNum": "800",
        "businessEntities": f"가상{n}구역 재개발정비사업조합",
        "telNo": "051-000-0000",
        "phone": "010-0000-0000",
        "contractor": "가상건설",
        "engineer": "미선정",
        "architect": "",
        "specialists": "가상정비",
        "viewImgPath": f"{IMG}/BARA_2026{n:04d}_view.jpg",
        "loctImgPath": f"{IMG}/BARA_2026{n:04d}_loct.jpg",
        "panoImgPath": "",
        "placeImgPath": None,
        "openInfoCnt": "3",
    }
    return {**row, **over}


def page_body(items, total: int | str | None = None, *, code: str = "00", msg: str = "NORMAL_CODE") -> dict:
    """items는 목록·객체 하나·빈 문자열·None 무엇이든 넣을 수 있다."""
    body = {"numOfRows": "100", "pageNo": "1", "totalCount": str(total if total is not None else len(items or []))}
    body["items"] = {"item": items} if isinstance(items, list | dict) else items
    return {"response": {"header": {"resultCode": code, "resultMsg": msg}, "body": body}}


class FakeRedevelopmentSource:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls_made = 0
        self.error: Exception | None = None

    async def zones(self) -> list[RedevelopmentItem]:
        self.calls_made += 1
        if self.error:
            raise self.error
        return [zone_from_row(r) for r in self.rows]

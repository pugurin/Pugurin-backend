import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date

from app.adapters.molit.types import QuotaExceeded, RawTrade, SourceAuthError, SourceKind
from app.repositories.ports import SourceUnavailable
from app.repositories.types import TradeMethod

# 공공데이터포털 공통 오류 코드. 22는 호출 한도, 나머지 인증·승인 계열은 키 문제로 본다
QUOTA_CODES = {"22"}
AUTH_CODES = {"20", "21", "30", "31", "32", "33"}


@dataclass(frozen=True)
class Page:
    total: int
    rows: list[RawTrade]


def parse_int(text: str | None) -> int | None:
    cleaned = (text or "").replace(",", "").strip()
    return int(cleaned) if cleaned.lstrip("-").isdigit() else None


def parse_float(text: str | None) -> float | None:
    cleaned = (text or "").replace(",", "").strip()
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def parse_short_date(text: str | None) -> date | None:
    """'25.11.12' 또는 '2025.11.12' 형태."""
    parts = (text or "").strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    year = int(parts[0]) + (2000 if len(parts[0]) == 2 else 0)
    try:
        return date(year, int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _raise_for_code(code: str, message: str) -> None:
    if code in QUOTA_CODES:
        raise QuotaExceeded(f"호출 한도 초과: {message}")
    if code in AUTH_CODES:
        raise SourceAuthError(f"인증 오류({code}): {message}")
    raise SourceUnavailable(f"원천 API 오류({code}): {message}")


def _row(kind: SourceKind, d: dict[str, str]) -> RawTrade:
    contract = date(int(d["dealYear"]), int(d["dealMonth"]), int(d["dealDay"]))
    method = {"중개거래": TradeMethod.broker, "직거래": TradeMethod.direct}.get(d.get("dealingGbn", ""))
    land_area = d.get("dealArea") if kind == SourceKind.land_trade else d.get("landAr")
    return RawTrade(
        kind=kind,
        sgg_cd=d.get("sggCd", ""),
        umd_nm=d.get("umdNm", ""),
        jibun=d.get("jibun", ""),
        contract_date=contract,
        umd_cd=d.get("umdCd") or None,
        name=(d.get("aptNm") or d.get("offiNm") or d.get("mhouseNm") or None),
        complex_seq=d.get("aptSeq") or None,
        apt_dong=d.get("aptDong") or None,
        build_year=parse_int(d.get("buildYear")),
        exclusive_area_m2=parse_float(d.get("excluUseAr")),
        land_area_m2=parse_float(land_area),
        floor=parse_int(d.get("floor")),
        amount_man=parse_int(d.get("dealAmount")),
        deposit_man=parse_int(d.get("deposit")),
        monthly_rent_man=parse_int(d.get("monthlyRent")),
        trade_method=method,
        is_cancelled=bool(d.get("cdealType")),
        cancelled_date=parse_short_date(d.get("cdealDay")),
        registered_date=parse_short_date(d.get("rgstDate")),
        jimok=d.get("jimok") or None,
        land_use=d.get("landUse") or None,
        house_type=d.get("houseType") or None,
        contract_type=d.get("contractType") or None,
        contract_term=d.get("contractTerm") or None,
        use_renewal_right=d.get("useRRRight") or None,
        land_leasehold=d.get("landLeaseholdGbn") == "Y",
        is_share_deal=bool(d.get("shareDealingType")),
        raw=d,
    )


def rows_from_fields(kind: SourceKind, items: list[dict[str, str]]) -> list[RawTrade]:
    """저장해 둔 원본 필드 목록에서 행을 다시 만든다."""
    return assign_ordinals([_row(kind, fields) for fields in items])


def parse_page(body: bytes, kind: SourceKind) -> Page:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise SourceUnavailable("원천 API 응답을 해석할 수 없습니다") from e

    if root.tag == "OpenAPI_ServiceResponse":  # 게이트웨이 단계 오류(키 미등록, 한도 초과 등)
        _raise_for_code(root.findtext(".//returnReasonCode") or "", root.findtext(".//errMsg") or "")

    code = (root.findtext(".//resultCode") or "").strip()
    if code != "000":
        _raise_for_code(code, root.findtext(".//resultMsg") or "")

    rows = []
    for item in root.findall(".//item"):
        fields = {child.tag: (child.text or "").strip() for child in item}
        try:
            rows.append(_row(kind, fields))
        except (KeyError, ValueError) as e:
            raise SourceUnavailable(f"거래일을 해석할 수 없는 행이 있습니다: {fields}") from e
    return Page(total=parse_int(root.findtext(".//totalCount")) or 0, rows=rows)


def assign_ordinals(rows: list[RawTrade]) -> list[RawTrade]:
    """완전히 같은 행(같은 날 같은 층 같은 금액 등)에 0, 1, 2… 순번을 붙여 source_key가 겹치지 않게 한다."""
    seen: Counter[tuple] = Counter()
    out = []
    for row in rows:
        out.append(replace(row, ordinal=seen[row.identity]))
        seen[row.identity] += 1
    return out

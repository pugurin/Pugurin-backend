from dataclasses import replace
from datetime import date

import pytest

from app.adapters.molit.mock import SAMPLE_DIR, FixtureTradeSource
from app.adapters.molit.parser import assign_ordinals, parse_float, parse_int, parse_page, parse_short_date
from app.adapters.molit.types import QuotaExceeded, SourceAuthError, SourceKind
from app.repositories.ports import SourceUnavailable
from app.repositories.types import DealType, PropertyType, TradeMethod

pytestmark = pytest.mark.anyio


async def rows(kind: SourceKind, lawd="26350"):
    return await FixtureTradeSource().fetch_month(kind, lawd, "202509")


def envelope(code="000", msg="OK", items="<items/>", total=0) -> bytes:
    return (
        f"<response><header><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header>"
        f"<body>{items}<numOfRows>1000</numOfRows><pageNo>1</pageNo><totalCount>{total}</totalCount></body></response>"
    ).encode()


def gateway_error(reason: str, msg: str) -> bytes:
    return (
        f"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>{msg}</errMsg>"
        f"<returnReasonCode>{reason}</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
    ).encode()


def test_value_parsers():
    assert (
        parse_int("144,000") == 144000 and parse_int(" 0 ") == 0 and parse_int("") is None and parse_int(None) is None
    )
    assert parse_float("84.842") == 84.842 and parse_float("") is None and parse_float("x") is None
    assert parse_short_date("25.11.12") == date(2025, 11, 12)
    assert parse_short_date("2025.11.12") == date(2025, 11, 12)
    assert parse_short_date("") is None and parse_short_date("25.13.40") is None


@pytest.mark.parametrize("kind", list(SourceKind))
@pytest.mark.parametrize("lawd", ["26350", "26500"])
async def test_every_fixture_parses_with_unique_keys(kind, lawd):
    parsed = await rows(kind, lawd)
    assert 5 <= len(parsed) <= 12
    assert len({r.source_key for r in parsed}) == len(parsed)
    for r in parsed:
        assert r.kind == kind and r.sgg_cd == lawd and r.umd_nm and r.jibun
        assert r.contract_date.year == 2025 and r.contract_date.month == 9
        assert r.property_type == kind.property_type
        assert len(r.source_key) == 40
        assert r.raw  # 원본 필드 보존


async def test_apartment_trade_row():
    parsed = await rows(SourceKind.apt_trade)
    first = parsed[0]
    assert (first.name, first.umd_nm, first.jibun) == ("해운대두산위브더제니스", "우동", "1407")
    assert first.contract_date == date(2025, 9, 30) and first.amount_man == 144000
    assert first.deal_type == DealType.sale and first.property_type == PropertyType.apartment
    assert first.complex_seq and first.complex_seq.startswith("26350-")
    assert first.umd_cd == "10500" and first.exclusive_area_m2 == 111.07 and first.floor == 14
    assert first.trade_method == TradeMethod.broker
    assert all(r.complex_seq for r in parsed)


async def test_cancelled_and_direct_trades_are_flagged():
    parsed = await rows(SourceKind.apt_trade)
    cancelled = [r for r in parsed if r.is_cancelled]
    assert len(cancelled) == 3 and all(r.cancelled_date for r in cancelled)
    assert any(r.trade_method == TradeMethod.direct for r in parsed)
    assert not any(r.is_cancelled or r.cancelled_date for r in parsed if r not in cancelled)


async def test_rent_rows_distinguish_jeonse_and_monthly():
    apt = await rows(SourceKind.apt_rent)
    assert apt[0].deal_type == DealType.jeonse and apt[0].deposit_man == 26000 and apt[0].monthly_rent_man == 0
    assert apt[0].amount_man is None and apt[0].complex_seq
    rh = await rows(SourceKind.rh_rent)
    monthly = [r for r in rh if r.deal_type == DealType.monthly]
    assert monthly and all(r.monthly_rent_man and r.deposit_man for r in monthly)
    assert monthly[0].contract_type == "신규" and monthly[0].contract_term == "25.09~27.09"


async def test_officetel_and_villa_have_no_complex_id():
    for kind in (SourceKind.offi_trade, SourceKind.offi_rent, SourceKind.rh_trade, SourceKind.rh_rent):
        parsed = await rows(kind)
        assert all(r.complex_seq is None and r.name for r in parsed), kind
    villa = (await rows(SourceKind.rh_trade))[0]
    assert villa.house_type == "연립" and villa.land_area_m2 == 92.98 and villa.property_type == PropertyType.villa


async def test_land_trade_has_masked_jibun_and_zoning():
    parsed = await rows(SourceKind.land_trade)
    assert all("*" in r.jibun for r in parsed)
    first = parsed[0]
    assert first.name is None and first.complex_seq is None
    assert first.jimok == "대" and first.land_use == "제1종일반주거지역"
    assert first.land_area_m2 == 542 and first.amount_man == 443000 and first.exclusive_area_m2 is None
    assert first.property_type == PropertyType.land


async def test_source_key_ignores_cancellation_and_registration_changes():
    base = (await rows(SourceKind.apt_trade))[0]
    later = replace(base, is_cancelled=True, cancelled_date=date(2025, 11, 1), registered_date=date(2026, 1, 5))
    assert later.source_key == base.source_key


async def test_source_key_changes_when_the_deal_changes():
    base = (await rows(SourceKind.apt_trade))[0]
    assert replace(base, amount_man=base.amount_man + 1).source_key != base.source_key
    assert replace(base, floor=base.floor + 1).source_key != base.source_key
    assert replace(base, contract_date=date(2025, 9, 1)).source_key != base.source_key
    assert replace(base, ordinal=1).source_key != base.source_key


async def test_identical_rows_get_distinct_ordinals():
    row = (await rows(SourceKind.apt_trade))[0]
    twins = assign_ordinals([row, row, row])
    assert [t.ordinal for t in twins] == [0, 1, 2]
    assert len({t.source_key for t in twins}) == 3


def test_empty_response_is_not_an_error():
    page = parse_page(envelope(), SourceKind.apt_trade)
    assert page.total == 0 and page.rows == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (envelope("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"), QuotaExceeded),
        (gateway_error("22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"), QuotaExceeded),
        (gateway_error("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"), SourceAuthError),
        (gateway_error("31", "DEADLINE_HAS_EXPIRED_ERROR"), SourceAuthError),
        (envelope("99", "UNKNOWN"), SourceUnavailable),
        (b"<html>502 Bad Gateway</html", SourceUnavailable),
    ],
)
def test_error_responses_map_to_specific_exceptions(body, error):
    with pytest.raises(error):
        parse_page(body, SourceKind.apt_trade)


def test_quota_and_auth_errors_are_source_unavailable():
    assert issubclass(QuotaExceeded, SourceUnavailable) and issubclass(SourceAuthError, SourceUnavailable)


def test_row_without_a_deal_date_is_rejected():
    items = "<items><item><sggCd>26350</sggCd><umdNm>우동</umdNm><jibun>1</jibun></item></items>"
    with pytest.raises(SourceUnavailable):
        parse_page(envelope(items=items, total=1), SourceKind.apt_trade)


def test_fixture_directory_has_all_kinds_for_two_districts():
    names = {p.name for p in SAMPLE_DIR.glob("*.xml")}
    assert len(names) == 14 and all(
        f"{k.value}_{g}_202509.xml" in names for k in SourceKind for g in ("26350", "26500")
    )

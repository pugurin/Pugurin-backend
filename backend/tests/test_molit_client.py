import httpx
import pytest

from app.adapters.molit.client import MolitClient
from app.adapters.molit.factory import create_trade_source
from app.adapters.molit.mock import FixtureTradeSource
from app.adapters.molit.types import QuotaExceeded, SourceAuthError, SourceKind
from app.core.config import Settings
from app.repositories.ports import SourceUnavailable

pytestmark = pytest.mark.anyio

TRADE_KEY, RENT_KEY = "trade-key", "rent-key"


def item(n: int) -> str:
    return (
        f"<item><aptNm>단지{n}</aptNm><aptSeq>26350-{n}</aptSeq><sggCd>26350</sggCd><umdNm>우동</umdNm><jibun>{n}</jibun>"
        f"<dealYear>2025</dealYear><dealMonth>9</dealMonth><dealDay>1</dealDay><dealAmount>{10_000 + n}</dealAmount>"
        "<excluUseAr>84.97</excluUseAr><floor>3</floor></item>"
    )


def page(start: int, count: int, total: int) -> bytes:
    items = "".join(item(n) for n in range(start, start + count)) or ""
    return (
        f"<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header><body><items>{items}</items>"
        f"<totalCount>{total}</totalCount></body></response>"
    ).encode()


class Recorder:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def make(handler, **kwargs) -> tuple[MolitClient, Recorder, list[float]]:
    recorder = Recorder(handler)
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    kwargs.setdefault("sleep", fake_sleep)
    return MolitClient(TRADE_KEY, RENT_KEY, base_url="https://example.test/api", http=http, **kwargs), recorder, delays


def paged(total: int):
    def handler(request: httpx.Request) -> httpx.Response:
        number, size = int(request.url.params["pageNo"]), int(request.url.params["numOfRows"])
        start = (number - 1) * size
        return httpx.Response(200, content=page(start, max(0, min(size, total - start)), total))

    return handler


async def test_request_shape_and_key_group():
    client, rec, _ = make(paged(1))
    await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    await client.fetch_month(SourceKind.offi_rent, "26350", "202509")
    trade, rent = rec.requests
    assert trade.url.path == "/api/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"
    assert dict(trade.url.params) == {
        "serviceKey": TRADE_KEY,
        "LAWD_CD": "26350",
        "DEAL_YMD": "202509",
        "pageNo": "1",
        "numOfRows": "1000",
    }
    assert rent.url.path.endswith("RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent")
    assert rent.url.params["serviceKey"] == RENT_KEY


async def test_paging_collects_every_page():
    client, rec, _ = make(paged(2500))
    rows = await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    assert len(rows) == 2500 and len({r.source_key for r in rows}) == 2500
    assert [r.url.params["pageNo"] for r in rec.requests] == ["1", "2", "3"]
    assert client.calls_made == 3


async def test_exact_page_boundary_does_not_request_an_extra_page():
    client, rec, _ = make(paged(1000))
    assert len(await client.fetch_month(SourceKind.apt_trade, "26350", "202509")) == 1000
    assert len(rec.requests) == 1


async def test_empty_month_returns_empty_list():
    client, rec, _ = make(paged(0))
    assert await client.fetch_month(SourceKind.apt_trade, "26350", "203001") == []
    assert len(rec.requests) == 1


async def test_server_error_is_retried_with_backoff():
    attempts = iter([httpx.Response(500), httpx.Response(502), httpx.Response(200, content=page(0, 1, 1))])
    client, rec, delays = make(lambda _: next(attempts), backoff_seconds=0.1)
    assert len(await client.fetch_month(SourceKind.apt_trade, "26350", "202509")) == 1
    assert len(rec.requests) == 3 and delays == [0.1, 0.2]
    assert client.calls_made == 3


async def test_network_error_is_retried_then_reported():
    def boom(_):
        raise httpx.ConnectError("끊김")

    client, rec, _ = make(boom, retries=2)
    with pytest.raises(SourceUnavailable):
        await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    assert len(rec.requests) == 3


async def test_unregistered_key_is_an_auth_error_and_is_not_retried():
    body = (
        "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>"
        "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
    )
    client, rec, _ = make(lambda _: httpx.Response(403, content=body.encode()))
    with pytest.raises(SourceAuthError):
        await client.fetch_month(SourceKind.apt_rent, "26350", "202509")
    assert len(rec.requests) == 1


async def test_non_xml_403_is_an_auth_error():
    client, _, _ = make(lambda _: httpx.Response(403, content=b"Forbidden"))
    with pytest.raises(SourceAuthError):
        await client.fetch_month(SourceKind.apt_trade, "26350", "202509")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429),
        httpx.Response(
            200, content=b"<response><header><resultCode>22</resultCode><resultMsg>x</resultMsg></header></response>"
        ),
    ],
)
async def test_quota_responses_raise_quota_exceeded(response):
    client, rec, _ = make(lambda _: response)
    with pytest.raises(QuotaExceeded):
        await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    assert len(rec.requests) == 1


async def test_daily_limit_stops_before_calling_the_api():
    client, rec, _ = make(paged(1), daily_limit=2)
    await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    await client.fetch_month(SourceKind.apt_trade, "26500", "202509")
    with pytest.raises(QuotaExceeded):
        await client.fetch_month(SourceKind.apt_trade, "26230", "202509")
    assert len(rec.requests) == 2 and client.calls_made == 2
    client.reset_calls()
    assert client.calls_made == 0
    await client.fetch_month(SourceKind.apt_trade, "26230", "202509")


async def test_retries_count_against_the_daily_limit():
    client, rec, _ = make(lambda _: httpx.Response(500), daily_limit=2, retries=5)
    with pytest.raises(QuotaExceeded):
        await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    assert len(rec.requests) == 2


async def test_missing_key_fails_without_calling_the_api():
    recorder = Recorder(lambda _: httpx.Response(200, content=page(0, 0, 0)))
    client = MolitClient(TRADE_KEY, "", http=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))
    await client.fetch_month(SourceKind.apt_trade, "26350", "202509")
    with pytest.raises(SourceAuthError):
        await client.fetch_month(SourceKind.apt_rent, "26350", "202509")
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("lawd", "ymd"), [("2635", "202509"), ("abcde", "202509"), ("26350", "2025-09"), ("26350", "20259")]
)
async def test_bad_arguments_are_rejected_before_any_call(lawd, ymd):
    client, rec, _ = make(paged(0))
    with pytest.raises(ValueError):
        await client.fetch_month(SourceKind.apt_trade, lawd, ymd)
    assert rec.requests == []


async def test_endless_pages_are_cut_off():
    client, _, _ = make(lambda r: httpx.Response(200, content=page(0, 1, 10_000_000)))
    with pytest.raises(SourceUnavailable):
        await client.fetch_month(SourceKind.apt_trade, "26350", "202509")


def test_factory_uses_fixtures_without_both_keys():
    assert isinstance(create_trade_source(Settings()), FixtureTradeSource)
    assert isinstance(create_trade_source(Settings(data_go_kr_trade_key="a")), FixtureTradeSource)
    assert isinstance(create_trade_source(Settings(data_go_kr_rent_key="b")), FixtureTradeSource)


def test_factory_uses_real_client_with_both_keys():
    source = create_trade_source(Settings(data_go_kr_trade_key="a", data_go_kr_rent_key="b", molit_daily_call_limit=5))
    assert isinstance(source, MolitClient)


async def test_fixture_source_returns_nothing_for_unknown_regions_and_months():
    source = FixtureTradeSource()
    assert await source.fetch_month(SourceKind.apt_trade, "26350", "202401") == []
    assert await source.fetch_month(SourceKind.apt_trade, "26110", "202509") == []
    assert source.calls_made == 0

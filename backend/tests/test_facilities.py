"""학교(나이스)·병원(심평원) 동기화: 원천 응답 해석, 지오코딩, 폐교·폐업 처리."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text

from app.adapters.facilities import HiraHospitalClient, NeisSchoolClient, hospital_from_row, school_from_row
from app.adapters.molit.types import QuotaExceeded, SourceAuthError
from app.db import migrate
from app.db.engine import make_engine
from app.ingestion.facilities import FACILITY_LOCK_KEY, sync_facilities
from app.ingestion.geocode import GeocodeCache
from app.ingestion.load import LoadBusy
from app.repositories.ports import SourceUnavailable
from tests.conftest import FIXED_NOW, create_database
from tests.fakes.madangs import FakeLookup

pytestmark = pytest.mark.anyio


def school(n: int, **over) -> dict:
    row = {
        "ATPT_OFCDC_SC_CODE": "C10",
        "SD_SCHUL_CODE": f"72400{n:02d}",
        "SCHUL_NM": f"가상{n}초등학교",
        "SCHUL_KND_SC_NM": "초등학교",
        "FOND_SC_NM": "공립",
        "COEDU_SC_NM": "남여공학",
        "ORG_RDNMA": f"부산광역시 해운대구 가상로 {n}",
        "ORG_RDNDA": "(우동)",
        "ORG_TELNO": "051-000-0000",
        "HMPG_ADRES": "http://",
        "FOAS_MEMRD": "19800301",
    }
    return {**row, **over}


def hospital(n: int, **over) -> dict:
    row = {
        "ykiho": f"JDQ4MTYx{n:04d}",
        "yadmNm": f"가상{n}의원",
        "clCdNm": "의원",
        "addr": f"부산광역시 수영구 광안로 {n}, 2층 (광안동)",
        "XPos": f"{129.11 + n / 10_000:.4f}",
        "YPos": f"{35.15 + n / 10_000:.4f}",
        "telno": "051-111-1111",
        "hospUrl": "https://example.kr/",
        "drTotCnt": 2,
        "estbDd": 20100105,
    }
    return {**row, **over}


# ── 원천 해석 ───────────────────────────────────────────────


def test_school_rows_keep_basic_fields_and_name_tentative_schools():
    s = school_from_row(school(1))
    assert (s.id, s.kind, s.homepage, s.opened_on.isoformat()) == ("7240001", "초등학교", None, "1980-03-01")
    tentative = school_from_row(
        school(2, SD_SCHUL_CODE="       ", SCHUL_NM="(가칭)명지6초등학교", FOAS_MEMRD="20290301")
    )
    assert tentative.id.startswith("TMP-") and tentative.opened_on.year == 2029
    assert school_from_row(school(2, SD_SCHUL_CODE=" ", SCHUL_NM="(가칭)명지6초등학교")).id == tentative.id


def test_hospital_rows_use_source_coordinates():
    h = hospital_from_row(hospital(1))
    assert (h.lat, h.lng, h.doctors, h.opened_on.isoformat()) == (35.1501, 129.1101, 2, "2010-01-05")


def http_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _no_sleep(_):
    return None


async def test_neis_pages_until_the_total_and_stops_on_no_data():
    def handler(request):
        page = int(request.url.params["pIndex"])
        assert request.url.params["ATPT_OFCDC_SC_CODE"] == "C10" and request.url.params["KEY"] == "k"
        if page > 2:
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})
        head = [{"list_total_count": 3}, {"RESULT": {"CODE": "INFO-000"}}]
        rows = [school(1), school(2)] if page == 1 else [school(3)]
        return httpx.Response(200, json={"schoolInfo": [{"head": head}, {"row": rows}]})

    client = NeisSchoolClient("k", http=http_client(handler), sleep=_no_sleep)
    assert [s.id for s in await client.schools()] == ["7240001", "7240002", "7240003"]


@pytest.mark.parametrize(("code", "error"), [("ERROR-290", SourceAuthError), ("ERROR-337", QuotaExceeded)])
async def test_neis_errors_are_mapped(code, error):
    client = NeisSchoolClient(
        "k", http=http_client(lambda r: httpx.Response(200, json={"RESULT": {"CODE": code, "MESSAGE": "x"}}))
    )
    with pytest.raises(error):
        await client.schools()


async def test_hira_pages_and_accepts_a_single_object_item():
    def handler(request):
        page = int(request.url.params["pageNo"])
        assert request.url.params["sidoCd"] == "210000" and request.url.params["numOfRows"] == "1000"
        item = [hospital(1), hospital(2)] if page == 1 else hospital(3)
        body = {"items": {"item": item}, "totalCount": 3}
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}, "body": body}})

    client = HiraHospitalClient("k", http=http_client(handler), sleep=_no_sleep)
    assert [h.name for h in await client.hospitals()] == ["가상1의원", "가상2의원", "가상3의원"]


async def test_hira_key_errors_in_xml_are_auth_errors():
    xml = "<OpenAPI_ServiceResponse><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    client = HiraHospitalClient("k", http=http_client(lambda r: httpx.Response(200, text=xml)), sleep=_no_sleep)
    with pytest.raises(SourceAuthError):
        await client.hospitals()


# ── 동기화 (실제 PostGIS) ─────────────────────────────────────


class FakeSchools:
    def __init__(self, rows):
        self.rows, self.calls_made, self.error = rows, 0, None

    async def schools(self):
        self.calls_made += 1
        if self.error:
            raise self.error
        return [school_from_row(r) for r in self.rows]


class FakeHospitals:
    def __init__(self, rows):
        self.rows, self.calls_made = rows, 0

    async def hospitals(self):
        self.calls_made += 1
        return [hospital_from_row(r) for r in self.rows]


@pytest.fixture(scope="module")
def facility_db(postgis: str) -> str:
    url = create_database(postgis, "facilities")
    migrate.upgrade(url)
    return url


@pytest.fixture
async def engine(facility_db):
    engine = make_engine(facility_db)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE schools, hospitals, etl_runs RESTART IDENTITY CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def cache(tmp_path) -> GeocodeCache:
    return GeocodeCache(tmp_path / "geocode.json")


async def sync(engine, schools, hospitals, cache, *, minutes=0, lookup=None):
    return await sync_facilities(
        engine, schools, hospitals, lookup or FakeLookup(), cache,
        now=FIXED_NOW + timedelta(minutes=minutes), out=lambda _: None,
    )  # fmt: skip


async def fetch(engine, sql: str):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql))).all()]


async def test_first_sync_stores_schools_with_geocoded_locations_and_hospitals_with_source_points(engine, cache):
    lookup = FakeLookup()
    report = await sync(engine, FakeSchools([school(1), school(2)]), FakeHospitals([hospital(1)]), cache, lookup=lookup)
    assert (report.status, report.schools, report.hospitals, report.inserted) == ("success", 2, 1, 3)
    assert lookup.queries == ["부산광역시 해운대구 가상로 1", "부산광역시 해운대구 가상로 2"]  # 병원은 원천 좌표
    assert await fetch(engine, "SELECT id, region_code, location IS NOT NULL FROM schools ORDER BY id") == [
        ("7240001", "2635010500", True),
        ("7240002", "2635010500", True),
    ]
    [(lat, lng)] = await fetch(engine, "SELECT ST_Y(location), ST_X(location) FROM hospitals")
    assert (round(lat, 4), round(lng, 4)) == (35.1501, 129.1101)
    assert await fetch(engine, "SELECT job, status FROM etl_runs") == [("facility", "success")]


async def test_second_sync_uses_the_geocode_cache_and_closes_what_disappeared(engine, cache):
    await sync(engine, FakeSchools([school(1), school(2)]), FakeHospitals([hospital(1), hospital(2)]), cache)
    lookup = FakeLookup()
    report = await sync(engine, FakeSchools([school(1)]), FakeHospitals([hospital(2)]), cache, minutes=1, lookup=lookup)
    assert lookup.queries == []
    assert (report.updated, report.closed) == (2, 2)
    assert await fetch(engine, "SELECT id FROM schools WHERE closed_at IS NULL") == [("7240001",)]
    back = await sync(engine, FakeSchools([school(1), school(2)]), FakeHospitals([hospital(2)]), cache, minutes=2)
    assert back.closed == 0
    assert await fetch(engine, "SELECT count(*) FROM schools WHERE closed_at IS NULL") == [(2,)]


async def test_a_failed_source_neither_writes_nor_closes_its_side(engine, cache):
    schools = FakeSchools([school(1)])
    await sync(engine, schools, FakeHospitals([hospital(1)]), cache)
    schools.error = SourceUnavailable("나이스 서버 오류(HTTP 503)")
    report = await sync(engine, schools, FakeHospitals([hospital(1)]), cache, minutes=1)
    assert report.status == "partial" and report.closed == 0
    assert await fetch(engine, "SELECT count(*) FROM schools WHERE closed_at IS NULL") == [(1,)]


async def test_hospitals_without_coordinates_are_geocoded_and_outside_busan_is_dropped(engine, cache):
    rows = [hospital(1, XPos="", YPos=""), hospital(2, XPos=None, YPos=None, addr="서울특별시 종로구 가상로 2")]
    report = await sync(engine, None, FakeHospitals(rows), cache)
    assert report.without_location == 1
    assert await fetch(engine, "SELECT name, location IS NOT NULL FROM hospitals ORDER BY name") == [
        ("가상1의원", True),
        ("가상2의원", False),
    ]


async def test_a_second_sync_refuses_to_run_while_one_is_in_progress(engine, cache):
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": FACILITY_LOCK_KEY})
        with pytest.raises(LoadBusy):
            await sync(engine, FakeSchools([]), None, cache)
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": FACILITY_LOCK_KEY})
    assert await fetch(engine, "SELECT count(*) FROM etl_runs") == [(0,)]

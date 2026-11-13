"""학구도(초등학교 통학구역) 파일 해석: DBF·SHP 읽기, 부산 구역 고르기, 구분 코드, 학교명 연결. DB 없이 돈다."""

from datetime import date

import pytest

from app.ingestion.school_zones import (
    SHARED_ZONE,
    ZONE,
    LinkRow,
    busan_links,
    busan_zones,
    find_zone_files,
    match_schools,
    read_dbf,
    read_links,
    read_polygons,
    rings_wkt,
    zone_kind,
)
from tests.fakes.schoolzone import ORIGIN_5186, FakeZone, link, square, write_zone_files


def _files(tmp_path):
    zones = [
        FakeZone("Z1", "해빛초통학구역", rings=[square(ORIGIN_5186, 300)]),
        FakeZone("Z2", "서울가상초통학구역", sd_cd="11", rings=[square(ORIGIN_5186, 100)]),
        FakeZone(
            "Z3",
            "가람초나래초공동통학구역",
            gb="1",
            rings=[square(ORIGIN_5186, 500), square(ORIGIN_5186, 100)[::-1]],  # 구멍 뚫린 구역
        ),
        FakeZone("Z4", "빈구역초통학구역"),  # null shape
    ]
    links = [
        link("Z1", "B1", "해빛초등학교"),
        link("Z2", "B2", "서울가상초등학교"),
        link("Z3", "B3", "가람초등학교"),
        link("Z3", "B4", "나래초등학교"),
        link("Z1", "B9", "해빛중학교", level="중학교"),
    ]
    return find_zone_files(write_zone_files(tmp_path, zones, links))


def test_finds_the_shapefile_and_the_link_csv(tmp_path):
    files = _files(tmp_path)
    assert files is not None
    assert files.shp.suffix == ".shp" and files.shx.exists() and files.dbf.exists() and files.links.suffix == ".csv"


def test_missing_files_are_reported_as_none(tmp_path):
    assert find_zone_files(tmp_path / "없음") is None
    (tmp_path / "zones").mkdir()
    assert find_zone_files(tmp_path) is None


def test_dbf_rows_are_decoded_as_cp949(tmp_path):
    rows = read_dbf(_files(tmp_path).dbf)
    assert [r["HAKGUDO_ID"] for r in rows] == ["Z1", "Z2", "Z3", "Z4"]
    assert rows[2]["HAKGUDO_NM"] == "가람초나래초공동통학구역" and rows[2]["HAKGUDO_GB"] == "1"
    assert rows[0]["OBJECTID"] == "1"


def test_only_busan_zones_are_kept_with_their_record_index(tmp_path):
    zones = busan_zones(read_dbf(_files(tmp_path).dbf))
    assert [(z.index, z.zone_id, z.kind) for z in zones] == [(0, "Z1", ZONE), (2, "Z3", SHARED_ZONE), (3, "Z4", ZONE)]
    assert zones[0].name == "해빛초통학구역" and zones[0].base_date == date(2026, 9, 20)


def test_polygon_rings_are_read_only_for_the_requested_records(tmp_path):
    files = _files(tmp_path)
    shapes = read_polygons(files.shp, files.shx, [0, 2, 3])
    assert set(shapes) == {0, 2, 3}
    assert shapes[0] == [square(ORIGIN_5186, 300)]
    assert len(shapes[2]) == 2 and shapes[2][1] == square(ORIGIN_5186, 100)[::-1]
    assert shapes[3] == []  # null shape


def test_rings_become_a_multilinestring_for_st_buildarea():
    wkt = rings_wkt([[(0, 0), (0, 1), (1, 1), (0, 0)], [(5, 5), (5, 6), (6, 6)]])
    assert (
        wkt
        == "MULTILINESTRING((0.000 0.000,0.000 1.000,1.000 1.000,0.000 0.000),(5.000 5.000,5.000 6.000,6.000 6.000,5.000 5.000))"
    )


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("0", "해빛초통학구역", "통학구역"),
        ("1", "만덕초상학초공동통학구역", "공동통학구역"),
        ("1", "남문초공덕초금성초공동(일방)통학구역", "공동통학구역"),
        # 모르는 코드면 이름으로 가린다
        ("9", "가람초나래초공동통학구역", "공동통학구역"),
        ("", "해빛초통학구역", "통학구역"),
    ],
)
def test_zone_kind_codes(code, name, expected):
    assert zone_kind(code, name) == expected


def test_links_keep_only_busan_elementary_rows(tmp_path):
    rows = read_links(_files(tmp_path).links)
    assert rows[0] == LinkRow("Z1", "B1", "해빛초등학교", "초등학교")
    kept = busan_links(rows, {"Z1", "Z3", "Z4"})
    assert [(r.zone_id, r.school_name) for r in kept] == [
        ("Z1", "해빛초등학교"),
        ("Z3", "가람초등학교"),
        ("Z3", "나래초등학교"),
    ]


def test_schools_are_matched_by_exact_name():
    links = [
        LinkRow("Z1", "B1", "해빛초등학교", "초등학교"),
        LinkRow("Z2", "B2", "가람초등학교", "초등학교"),
        LinkRow("Z3", "B2", "가람초등학교", "초등학교"),  # 한 학교가 두 구역
        LinkRow("Z4", "B5", "연학초등학교", "초등학교"),  # 우리 목록에 없다
        # 학구도 쪽에 같은 이름 학교가 둘(학교ID가 다르다)
        LinkRow("Z5", "B6", "송정초등학교", "초등학교"),
        LinkRow("Z6", "B7", "송정초등학교", "초등학교"),
        LinkRow("Z7", "B8", "다솜초등학교", "초등학교"),
    ]
    ours = [
        ("7240001", "해빛초등학교"),
        ("7240002", "가람초등학교"),
        ("7240006", "송정초등학교"),
        ("7240008", "다솜초등학교"),
        ("7240009", "다솜초등학교"),  # 우리 쪽에 같은 이름이 둘
    ]
    m = match_schools(links, ours)
    assert m.pairs == [("Z1", "7240001"), ("Z2", "7240002"), ("Z3", "7240002")]
    assert m.unmatched == ["연학초등학교"]
    assert m.ambiguous == ["다솜초등학교", "송정초등학교"]

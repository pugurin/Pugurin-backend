"""브이월드 정비구역 경계 파일 해석과 우리 구역 잇기 규칙. DB 없이 돈다."""

from datetime import date

import pytest

from app.ingestion.redevelopment_zones import (
    busan_zone_records,
    core_name,
    default_zone_dir,
    find_zone_files,
    link_pairs,
)
from app.ingestion.school_zones import read_dbf
from tests.fakes.redevelopment_zones import FakeZone, write_zone_files
from tests.fakes.schoolzone import ORIGIN_5186, square


def _rows(tmp_path):
    zones = [
        FakeZone("Z1", name="가상1주택재개발정비구역", rings=[square(ORIGIN_5186, 300)]),
        FakeZone("Z2", name="", note="", rings=[square(ORIGIN_5186, 200)]),  # 이름 없음
        FakeZone("Z3", name="부산광역시 고시 제2016-415호", rings=[square(ORIGIN_5186, 200)]),
        FakeZone("Z4", name="정비기반시설(도로)", rings=[square(ORIGIN_5186, 50)]),  # 부속 시설
        FakeZone("Z5", name="", note="공공보행통로", rings=[square(ORIGIN_5186, 50)]),  # 부속 시설
        FakeZone("Z6", name="효제1구역", sigungu="11110", rings=[square(ORIGIN_5186, 100)]),  # 서울
        FakeZone("Z7", name="(자세한사항은 별도문의 : 건축과)", designated="", rings=[square(ORIGIN_5186, 100)]),
        FakeZone("Z8", name="공동주택용지", rings=[square(ORIGIN_5186, 100)]),  # 부속 시설
        # 실제 파일은 빈 칸을 NUL(\x00)로 채운다
        FakeZone(
            "Z10",
            name="수안1 재건축 정비구역\r\n토지정보과-8259(2022.06.22.)호로 정정",
            rings=[square(ORIGIN_5186, 90)],
        ),
        FakeZone("Z9", name="\x00" * 20, note="\x00" * 20, designated="\x00" * 8, rings=[square(ORIGIN_5186, 100)]),
    ]
    files = find_zone_files(write_zone_files(tmp_path, zones))
    assert files is not None
    return read_dbf(files.dbf)


def test_only_busan_zones_without_facilities_are_kept_with_their_record_index(tmp_path):
    zones = busan_zone_records(_rows(tmp_path))
    assert [(z.index, z.zone_id) for z in zones] == [(0, "Z1"), (1, "Z2"), (2, "Z3"), (6, "Z7"), (8, "Z10"), (9, "Z9")]


def test_names_notices_and_dates_are_cleaned(tmp_path):
    by_id = {z.zone_id: z for z in busan_zone_records(_rows(tmp_path))}
    assert (by_id["Z1"].name, by_id["Z1"].notice) == ("가상1주택재개발정비구역", None)
    assert (by_id["Z2"].name, by_id["Z2"].notice) == (None, None)
    # 고시문만 있으면 이름이 아니라 고시 번호로 둔다
    assert (by_id["Z3"].name, by_id["Z3"].notice) == (None, "부산광역시 고시 제2016-415호")
    # "별도문의" 안내 문구는 이름이 아니다
    assert by_id["Z7"].name is None and by_id["Z7"].designated_on is None
    assert (by_id["Z9"].name, by_id["Z9"].notice, by_id["Z9"].designated_on) == (None, None, None)
    # 이름 뒤 줄바꿈으로 붙은 정정 메모는 떼어 낸다
    assert by_id["Z10"].name == "수안1 재건축 정비구역"
    assert by_id["Z1"].designated_on == date(2020, 1, 1) and by_id["Z1"].base_date == date(2026, 11, 6)
    assert by_id["Z1"].sigungu_code == "26230"


def test_missing_files_are_reported_as_none(tmp_path):
    assert find_zone_files(tmp_path / "없음") is None
    (tmp_path / "zones").mkdir()
    assert find_zone_files(tmp_path) is None


def test_default_dir_is_found_from_the_backend_folder_or_its_parent(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    assert default_zone_dir(backend) == tmp_path / "data" / "redevelopment"
    (backend / "data" / "redevelopment").mkdir(parents=True)
    assert default_zone_dir(backend) == backend / "data" / "redevelopment"


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        ("광안2주택재건축구역", "광안2"),
        ("광안2 재개발", "광안2"),
        ("영주1 재건축 정비구역", "영주1"),
        ("초량1-3 도시환경정비구역", "초량1-3"),
        ("연산5재개발사업정비구역", "연산5"),
        ("서대신7주택재개발", "서대신7"),
        ("아미4 주거환경개선 정비구역", "아미4"),
        ("시민공원주변재정비촉진1구역 재개발", "시민공원주변1"),
        ("주택재개발정비구역", ""),
        # 문장부호는 무시한다(재정비촉진구역 이름이 양쪽에서 다르게 적힌다)
        ("서,금사 재정비촉진(A)구역", "서금사A"),
        ("서금사재정비촉진A구역 재개발", "서금사A"),
    ],
)
def test_core_name_strips_business_words(raw, core):
    assert core_name(raw) == core


OURS = [
    ("R1", "가상1 재개발", "redevelopment", "가상동 101"),
    ("R2", "가상2 재건축", "reconstruction", "가상동 202"),
    ("R3", "가상3 가로주택정비", "street_housing", None),  # 정비법 구역이 아니라 잇지 않는다
    ("R4", "같은이름 재개발", "redevelopment", None),
    ("R5", "같은이름 재개발", "redevelopment", None),  # 우리 쪽 핵심 이름이 겹친다
    ("R6", "가상6 재개발", "redevelopment", None),
]
ZONES = [
    ("Z1", "가상1주택재개발정비구역"),
    ("Z2", None),
    ("Z3", "가상3가로주택"),
    ("Z4", "같은이름재개발구역"),
    ("Z6", "가상6재개발구역"),
    ("Z7", "가상6 재개발 정비구역"),  # 경계 쪽 핵심 이름이 겹친다
]


def test_points_link_first_then_unique_names():
    # 좌표로 R2 → Z2(가장 큰 것), R1 → Z1. 이름으로는 R1이 이미 이어졌다
    hits = [("R2", "Z2", 90000.0), ("R2", "Z1", 5000.0), ("R1", "Z1", 5000.0)]
    pairs = link_pairs(hits, OURS, ZONES)
    assert pairs == {"R1": ("Z1", "point"), "R2": ("Z2", "point")}


def test_names_link_when_both_sides_are_unique_and_the_kind_is_linkable():
    pairs = link_pairs([], OURS, ZONES)
    assert pairs == {"R1": ("Z1", "name")}  # R3 가로주택, R4·R5 겹침, R6 경계 겹침은 잇지 않는다


def test_a_zone_takes_only_one_of_ours():
    hits = [("R1", "Z1", 100.0), ("R6", "Z1", 100.0)]
    pairs = link_pairs(hits, OURS, ZONES)
    assert pairs == {"R1": ("Z1", "point")}  # 같은 경계에 둘이 들면 ID 순으로 하나만


OFFICE_OURS = [
    ("O1", "대연5 재개발", "redevelopment", "용소로28번길 8, 7층 (대연동,금하빌딩)"),
    ("O2", "망미1 재개발", "redevelopment", "수영구 광안로 10, 2층"),  # 사무실 동네(광안)가 구역 이름과 다르다
    ("O3", "구포3 재개발", "redevelopment", "구포동 백양대로 1139, 2층"),
    ("O4", "구포8 재개발", "redevelopment", "구포동 백양대로 1139, 3층"),  # 같은 건물에 다른 조합
]


def test_offices_link_only_inside_an_unlinked_zone_whose_name_matches_the_neighbourhood():
    offices = [("O1", "ZO1", 9000.0), ("O2", "ZO2", 9000.0), ("O3", "ZO3", 9000.0), ("O4", "ZO3", 9000.0)]
    zones = [("ZO1", None), ("ZO2", None), ("ZO3", None)]
    pairs = link_pairs([], OFFICE_OURS, zones, office_hits=offices)
    # O2는 사무실 동네가 이름과 달라서, O3·O4는 한 경계에 둘이 들어서 잇지 않는다
    assert pairs == {"O1": ("ZO1", "office")}


def test_points_and_names_come_before_offices():
    offices = [("O1", "ZO1", 9000.0)]
    pairs = link_pairs([("R1", "ZO1", 9000.0)], [*OURS, *OFFICE_OURS], [("ZO1", None)], office_hits=offices)
    assert pairs == {"R1": ("ZO1", "point")}

"""전세·월세 갱신 계약은 기본으로 빼고 include_renewals=true면 넣는다 (ERD 3.6, API 명세 §4).

인상률 5% 상한 때문에 갱신 계약은 지금 시세를 반영하지 않는다. 실제 응답 샘플(해운대구·수영구 2025-09)로 확인한다.
"""

import pytest

from tests.conftest import API
from tests.test_ingestion import built, served

pytestmark = pytest.mark.anyio

BUSAN = "128.75,34.85,129.35,35.40"


async def renewals(tmp_path):
    trades, dataset, _, _ = await built(tmp_path)
    renewed = [t for t in dataset.transactions if t.is_renewal]
    assert renewed and all(t.deal_type != "sale" for t in renewed)
    return dataset, renewed


async def test_renewals_are_marked_on_each_transaction(tmp_path):
    dataset, renewed = await renewals(tmp_path)
    with await served(tmp_path) as c:
        target = renewed[0]
        body = c.get(f"{API}/transactions/{target.id}").json()["data"]
        assert body["is_renewal"] is True
        sale = next(t for t in dataset.transactions if t.deal_type == "sale")
        assert c.get(f"{API}/transactions/{sale.id}").json()["data"]["is_renewal"] is False


async def test_transaction_lists_leave_renewals_out_unless_asked(tmp_path):
    _, renewed = await renewals(tmp_path)
    t = renewed[0]
    query = {
        "property_type": t.property_type.value,
        "deal_type": t.deal_type.value,
        "period_months": 24,
        "page_size": 100,
        "bbox": BUSAN,
    }
    with await served(tmp_path) as c:
        default = c.get(f"{API}/transactions", params=query).json()
        everything = c.get(f"{API}/transactions", params=query | {"include_renewals": "true"}).json()
    same_kind = [r for r in renewed if (r.property_type, r.deal_type) == (t.property_type, t.deal_type)]
    assert everything["meta"]["total"] - default["meta"]["total"] == len(same_kind)
    assert not any(row["is_renewal"] for row in default["data"])


async def test_map_counts_and_complex_history_follow_the_same_default(tmp_path):
    _, renewed = await renewals(tmp_path)
    t = next(r for r in renewed if r.complex_id)
    params = {"bbox": BUSAN, "zoom": 11, "property_type": t.property_type.value, "deal_type": t.deal_type.value}
    params["period_months"] = 24
    with await served(tmp_path) as c:
        default = sum(
            m["transaction_count"] for m in c.get(f"{API}/map/markers", params=params).json()["data"]["markers"]
        )
        params["include_renewals"] = "true"
        everything = sum(
            m["transaction_count"] for m in c.get(f"{API}/map/markers", params=params).json()["data"]["markers"]
        )
        assert everything > default

        history = f"{API}/complexes/{t.complex_id}/transactions"
        ids = {row["id"] for row in c.get(history, params={"deal_type": t.deal_type.value}).json()["data"]}
        assert str(t.id) not in ids
        ids = {
            row["id"]
            for row in c.get(history, params={"deal_type": t.deal_type.value, "include_renewals": "true"}).json()[
                "data"
            ]
        }
        assert str(t.id) in ids


async def test_stats_exclude_renewals_by_default_and_say_so(tmp_path):
    _, renewed = await renewals(tmp_path)
    t = renewed[0]
    url = f"{API}/stats/regions/{t.region_code[:5]}"
    params = {"property_type": t.property_type.value, "deal_type": t.deal_type.value, "period_months": 24}
    with await served(tmp_path) as c:
        default = c.get(url, params=params).json()
        everything = c.get(url, params=params | {"include_renewals": "true"}).json()
        sale = c.get(url, params=params | {"deal_type": "sale"}).json()
    assert everything["data"]["transaction_count"] > default["data"]["transaction_count"]
    assert default["meta"]["method"] == "중위값, 해제거래 제외, 갱신 계약 제외"
    assert everything["meta"]["method"] == "중위값, 해제거래 제외"
    # 매매에는 갱신 계약이 없으니 문구도 붙이지 않는다
    assert sale["meta"]["method"] == "중위값, 해제거래 제외"


async def test_complex_stats_and_analysis_follow_the_same_default(tmp_path):
    _, renewed = await renewals(tmp_path)
    t = next(r for r in renewed if r.complex_id)
    with await served(tmp_path) as c:
        url = f"{API}/complexes/{t.complex_id}/stats"
        params = {"property_type": t.property_type.value, "deal_type": t.deal_type.value, "period_months": 24}
        default = c.get(url, params=params).json()
        everything = c.get(url, params=params | {"include_renewals": "true"}).json()
        assert everything["data"]["transaction_count"] > default["data"]["transaction_count"]

        analysis = f"{API}/analysis/complexes/{t.complex_id}"
        params = {"deal_type": t.deal_type.value, "period_months": 24}
        assert c.get(analysis, params=params).json()["meta"]["method"].endswith("갱신 계약 제외")
        assert (
            not c.get(analysis, params=params | {"include_renewals": "true"})
            .json()["meta"]["method"]
            .endswith("갱신 계약 제외")
        )

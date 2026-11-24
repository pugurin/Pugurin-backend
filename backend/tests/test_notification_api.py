"""알림함 API (실제 PostGIS). 알림은 리포트 배치 대신 repo `insert_report`로 직접 넣는다."""

import asyncio
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import migrate
from app.db.engine import make_engine
from app.repositories.postgres.notifications import PostgresNotificationRepository
from tests.conftest import API, FIXED_NOW, create_database
from tests.test_interest_api import app_for, client

MONDAY = date(2026, 11, 16)


@pytest.fixture(scope="module")
def notification_db(postgis: str) -> str:
    url = create_database(postgis, "notification_api")
    migrate.upgrade(url)
    return url


def insert_reports(url: str, user_id: str, weeks: int = 3) -> list[bool]:
    """주 단위 리포트를 weeks통 넣는다(가장 오래된 주부터)."""

    async def run() -> list[bool]:
        engine = make_engine(url)
        repo = PostgresNotificationRepository(engine)
        results = [
            await repo.insert_report(
                UUID(user_id),
                MONDAY - timedelta(weeks=weeks - 1 - i),
                f"리포트 {i}",
                f"요약 {i}",
                {"regions": [{"code": "1111011000", "name": "청운동"}]},
                FIXED_NOW + timedelta(minutes=i),
            )
            for i in range(weeks)
        ]
        await engine.dispose()
        return results

    return asyncio.run(run())


def my_id(c: TestClient) -> str:
    return c.get(f"{API}/users/me").json()["data"]["id"]


def test_list_is_newest_first_with_unread_count(notification_db):
    with client(notification_db) as c:
        insert_reports(notification_db, my_id(c))
        r = c.get(f"{API}/notifications")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "private, no-cache"
        body = r.json()
        assert [n["title"] for n in body["data"]] == ["리포트 2", "리포트 1", "리포트 0"]
        assert body["meta"]["unread_count"] == 3
        assert body["meta"]["total"] == 3
        assert "payload" not in body["data"][0]
        assert body["data"][0]["kind"] == "interest_report"
        assert body["data"][0]["read_at"] is None

        page = c.get(f"{API}/notifications", params={"page": 2, "page_size": 2}).json()
        assert [n["title"] for n in page["data"]] == ["리포트 0"]
        assert (page["meta"]["page"], page["meta"]["total_pages"]) == (2, 2)
        assert c.get(f"{API}/notifications", params={"page_size": 51}).status_code == 400


def test_detail_and_read(notification_db):
    with client(notification_db) as c:
        insert_reports(notification_db, my_id(c))
        items = c.get(f"{API}/notifications").json()["data"]
        first = items[0]["id"]

        detail = c.get(f"{API}/notifications/{first}")
        assert detail.status_code == 200
        assert detail.headers["Cache-Control"] == "private, no-cache"
        assert detail.json()["data"]["payload"]["regions"][0]["name"] == "청운동"

        r = c.post(f"{API}/notifications/read", json={"ids": [first]})
        assert r.status_code == 204
        assert r.headers["Cache-Control"] == "private, no-cache"
        listed = c.get(f"{API}/notifications").json()
        assert listed["meta"]["unread_count"] == 2
        assert listed["data"][0]["read_at"] is not None

        assert c.post(f"{API}/notifications/read", json={"all": True}).status_code == 204
        assert c.get(f"{API}/notifications").json()["meta"]["unread_count"] == 0


def test_read_needs_ids_or_all(notification_db):
    with client(notification_db) as c:
        assert c.post(f"{API}/notifications/read", json={}).status_code == 400
        assert c.post(f"{API}/notifications/read", json={"ids": [], "all": False}).status_code == 400


def test_other_users_notification_is_404(notification_db):
    with client(notification_db) as owner, client(notification_db) as other:
        insert_reports(notification_db, my_id(owner), weeks=1)
        nid = owner.get(f"{API}/notifications").json()["data"][0]["id"]
        assert other.get(f"{API}/notifications/{nid}").status_code == 404
        assert other.get(f"{API}/notifications/{uuid4()}").status_code == 404
        # 남의 알림은 읽음 처리도 되지 않는다
        assert other.post(f"{API}/notifications/read", json={"ids": [nid]}).status_code == 204
        assert owner.get(f"{API}/notifications").json()["meta"]["unread_count"] == 1
        assert other.get(f"{API}/notifications").json()["data"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/notifications"), ("GET", f"/notifications/{uuid4()}"), ("POST", "/notifications/read")],
)
def test_notifications_need_login(notification_db, method, path):
    with TestClient(app_for(notification_db)) as c:
        r = c.request(method, f"{API}{path}")
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UNAUTHORIZED")


def test_weekly_report_setting_roundtrip(notification_db):
    with client(notification_db) as c:
        r = c.get(f"{API}/notifications/settings")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "private, no-cache"
        assert r.json() == {"data": {"weekly_report": True}}  # 새 회원은 기본 받음

        r = c.put(f"{API}/notifications/settings", json={"weekly_report": False})
        assert (r.status_code, r.json()) == (200, {"data": {"weekly_report": False}})
        assert c.get(f"{API}/notifications/settings").json()["data"]["weekly_report"] is False
        assert c.get(f"{API}/users/me").json()["data"]["settings"]["weekly_report"] is False

        # 다른 설정 키를 건드리지 않는다
        assert c.get(f"{API}/users/me").json()["data"]["settings"]["loading_tips"] is True

        # PATCH /users/me로 다시 켠다
        assert c.patch(f"{API}/users/me", json={"settings": {"weekly_report": True}}).status_code == 200
        assert c.get(f"{API}/notifications/settings").json()["data"]["weekly_report"] is True


def test_weekly_report_setting_rejects_other_keys(notification_db):
    with client(notification_db) as c:
        r = c.put(f"{API}/notifications/settings", json={"weekly_report": False, "loading_tips": False})
        assert r.status_code == 400
        assert c.put(f"{API}/notifications/settings", json={}).status_code == 400


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_weekly_report_setting_needs_login(notification_db, method):
    with TestClient(app_for(notification_db)) as c:
        r = c.request(
            method, f"{API}/notifications/settings", json={"weekly_report": True} if method == "PUT" else None
        )
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UNAUTHORIZED")


def test_same_week_inserts_once(notification_db):
    with client(notification_db) as c:
        uid = my_id(c)
        assert insert_reports(notification_db, uid, weeks=1) == [True]
        assert insert_reports(notification_db, uid, weeks=1) == [False]
        assert c.get(f"{API}/notifications").json()["meta"]["total"] == 1


def test_bounds_on_page_and_ids(notification_db):
    with client(notification_db) as c:
        assert c.get(f"{API}/notifications?page=1001").status_code == 400
        assert c.get(f"{API}/notifications?page=1000").status_code == 200
        too_many = [str(uuid4()) for _ in range(101)]
        assert c.post(f"{API}/notifications/read", json={"ids": too_many}).status_code == 400
        assert c.post(f"{API}/notifications/read", json={"ids": too_many[:100]}).status_code == 204


def test_insert_report_skips_withdrawn_users(notification_db):
    with client(notification_db) as c:
        uid = my_id(c)
        assert c.delete(f"{API}/users/me").status_code == 204
    assert insert_reports(notification_db, uid, weeks=1) == [False]

    async def rows() -> int:
        engine = make_engine(notification_db)
        async with engine.connect() as conn:
            n = (
                await conn.execute(text("SELECT count(*) FROM notifications WHERE user_id = :id"), {"id": uid})
            ).scalar()
        await engine.dispose()
        return n

    assert asyncio.run(rows()) == 0


def test_delete_older_than(notification_db):
    async def run(user_id: str) -> int:
        engine = make_engine(notification_db)
        n = await PostgresNotificationRepository(engine).delete_older_than(FIXED_NOW + timedelta(minutes=1))
        await engine.dispose()
        return n

    with client(notification_db) as c:
        uid = my_id(c)
        insert_reports(notification_db, uid, weeks=3)  # created_at: +0, +1, +2분
        assert asyncio.run(run(uid)) >= 1  # +0분(리포트 0)만 컷오프보다 오래됐다
        left = c.get(f"{API}/notifications").json()["data"]
        assert [n["title"] for n in left] == ["리포트 2", "리포트 1"]


def test_withdraw_deletes_notifications(notification_db):
    with client(notification_db) as c:
        uid = my_id(c)
        insert_reports(notification_db, uid)
        assert c.delete(f"{API}/users/me").status_code == 204

    async def rows() -> int:
        engine = make_engine(notification_db)
        async with engine.connect() as conn:
            n = (
                await conn.execute(text("SELECT count(*) FROM notifications WHERE user_id = :id"), {"id": uid})
            ).scalar()
        await engine.dispose()
        return n

    assert asyncio.run(rows()) == 0


def test_without_a_database_notifications_are_503():
    with client(None) as c:
        assert c.get(f"{API}/notifications", headers={"Authorization": "Bearer x"}).status_code == 503

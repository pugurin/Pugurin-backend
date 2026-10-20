from sqlalchemy import Connection, text

from app.db.legal_dong import LegalDong, load_legal_dongs

UPSERT_REGION = text(
    """
    INSERT INTO regions (code, level, parent_code, name)
    VALUES (:code, :level, :parent_code, :name)
    ON CONFLICT (code) DO UPDATE
        SET level = EXCLUDED.level, parent_code = EXCLUDED.parent_code, name = EXCLUDED.name
    """
)


def upsert_regions(conn: Connection, dongs: list[LegalDong] | None = None) -> int:
    """법정동 마스터를 regions에 넣는다. 여러 번 실행해도 결과가 같다. 중심점(centroid)은 건드리지 않는다."""
    rows = dongs if dongs is not None else load_legal_dongs()
    conn.execute(
        UPSERT_REGION,
        [{"code": d.code, "level": d.level, "parent_code": d.parent_code, "name": d.name} for d in rows],
    )
    return len(rows)

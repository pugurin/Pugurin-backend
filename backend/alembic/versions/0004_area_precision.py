"""면적을 소수 4자리까지 저장 (원천 excluUseAr·dealArea가 4자리까지 온다, 약 30%)

basis_area_m2(생성 컬럼)와 mv_monthly_stats가 면적 컬럼에 기대고 있어 지웠다가 다시 만든다.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

MONTHLY_STATS = """
CREATE MATERIALIZED VIEW mv_monthly_stats AS
WITH base AS (
    SELECT t.*, date_trunc('month', t.contract_date)::date AS month
    FROM transactions t
    WHERE t.deleted_at IS NULL AND NOT t.is_cancelled
      AND NOT t.is_share_deal AND coalesce(t.jimok, '') <> '도로'
), scoped AS (
    SELECT 'dong' AS scope_level, region_code::text AS scope_id, * FROM base
    UNION ALL
    SELECT 'sigungu', left(region_code, 5), * FROM base
    UNION ALL
    SELECT 'complex', complex_id::text, * FROM base WHERE complex_id IS NOT NULL
)
SELECT s.scope_level, s.scope_id, s.property_type, s.deal_type, s.month, d.include_direct, r.include_renewals,
       count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (s.price / (s.basis_area_m2 / 3.3058))::double precision)
           FILTER (WHERE s.deal_type = 'sale') AS median_price_per_pyeong,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY (s.deposit / (s.basis_area_m2 / 3.3058))::double precision)
           FILTER (WHERE s.deal_type = 'jeonse') AS median_deposit_per_pyeong,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.deposit::double precision)
           FILTER (WHERE s.deal_type = 'monthly') AS median_deposit,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.monthly_rent::double precision)
           FILTER (WHERE s.deal_type = 'monthly') AS median_monthly_rent
FROM scoped s
CROSS JOIN LATERAL (VALUES (true), (false)) AS d(include_direct)
CROSS JOIN LATERAL (VALUES (true), (false)) AS r(include_renewals)
WHERE (d.include_direct OR s.trade_method IS DISTINCT FROM 'direct')
  AND (r.include_renewals OR s.contract_type IS DISTINCT FROM '갱신')
GROUP BY s.scope_level, s.scope_id, s.property_type, s.deal_type, s.month, d.include_direct, r.include_renewals;
CREATE UNIQUE INDEX mv_monthly_stats_key
    ON mv_monthly_stats (scope_level, scope_id, property_type, deal_type, month, include_direct, include_renewals);
"""


def _widen(exclusive: str, land: str) -> list[str]:
    return [
        "DROP MATERIALIZED VIEW mv_monthly_stats",
        "ALTER TABLE transactions DROP COLUMN basis_area_m2",
        f"ALTER TABLE transactions ALTER COLUMN exclusive_area_m2 TYPE {exclusive}, "
        f"ALTER COLUMN land_area_m2 TYPE {land}",
        "ALTER TABLE transactions ADD COLUMN basis_area_m2 numeric "
        "GENERATED ALWAYS AS (coalesce(exclusive_area_m2, land_area_m2)) STORED",
        *[s.strip() for s in MONTHLY_STATS.split(";\n") if s.strip()],
    ]


def upgrade() -> None:
    for statement in _widen("numeric(10, 4)", "numeric(14, 4)"):
        op.execute(statement)


def downgrade() -> None:
    for statement in _widen("numeric(9, 3)", "numeric(12, 2)"):
        op.execute(statement)

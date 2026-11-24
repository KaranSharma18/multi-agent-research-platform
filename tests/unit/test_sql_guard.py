from __future__ import annotations

import pytest
from mara_mcp_warehouse.guard import SQLRejected, check_sql

VIEWS = {"customers", "subscriptions", "invoices", "incidents", "incident_impacts", "usage_daily"}


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from analytics.customers where segment = 'enterprise'",
        "SELECT name, arr_usd FROM analytics.customers ORDER BY arr_usd DESC LIMIT 5",
        "with hit as (select distinct customer_id from analytics.incident_impacts where incident_id = 'INC-2041') "
        "select sum(c.arr_usd) from analytics.customers c join hit on hit.customer_id = c.id",
        "select c.name, sum(c.arr_usd) over () as total from analytics.customers c",
        "select * from analytics.invoices;",
    ],
)
def test_allows_read_only_selects_over_views(sql: str) -> None:
    assert check_sql(sql, VIEWS)


@pytest.mark.parametrize(
    "sql",
    [
        "delete from analytics.customers",
        "update analytics.customers set arr_usd = 0",
        "insert into analytics.customers values ('x')",
        "drop table analytics.customers",
        "select 1; drop table analytics.customers",
        "select billing_email from core.customers",
        "select * from pg_catalog.pg_user",
        "select pg_sleep(10)",
        "select set_config('app.regions', 'all', false)",
        "select current_setting('app.regions')",
        "select * from analytics.secret_table",
        "copy analytics.customers to '/tmp/x'",
        "truncate analytics.invoices",
    ],
)
def test_rejects_writes_other_schemas_and_dangerous_functions(sql: str) -> None:
    with pytest.raises(SQLRejected):
        check_sql(sql, VIEWS)

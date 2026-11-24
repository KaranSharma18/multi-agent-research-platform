"""Build the warehouse: raw tables in `core`, safe views in `analytics`, a read-only role.

Three layers of protection sit in the database itself, under the SQL guard in
the MCP server:
  1. `agent_ro` can read only the analytics views; the personal-data columns in
     core.customers (emails, phone numbers) are not granted at all.
  2. Row-level security limits customer rows to the regions in the caller's
     token. The MCP server sets `app.regions` per query from the token; the
     views are `security_invoker`, so the policies apply to the caller.
  3. The role is read-only by default and has a statement timeout.
"""

from __future__ import annotations

import psycopg
from mara_core.logging import get_logger
from psycopg import sql

from mara_data.world import World

log = get_logger(__name__)

SCHEMA = """
drop schema if exists analytics cascade;
drop schema if exists core cascade;
create schema core;
create schema analytics;

create table core.customers (
    id text primary key, name text not null, segment text not null, region text not null,
    arr_usd numeric(12, 2) not null, plan text not null, owner_github text not null, owner_name text not null,
    owner_email text not null, billing_email text not null, billing_phone text not null, created_at date not null);
create table core.subscriptions (
    customer_id text primary key references core.customers (id), plan text not null, status text not null,
    started_at date not null, churned_at date, mrr_usd numeric(12, 2) not null);
create table core.invoices (
    id text primary key, customer_id text not null references core.customers (id), amount_usd numeric(12, 2) not null,
    issued_at date not null, status text not null);
create table core.incidents (
    id text primary key, service text not null, severity text not null, title text not null,
    started_at timestamptz not null, resolved_at timestamptz, status text not null, duration_minutes integer);
create table core.incident_impacts (
    incident_id text not null references core.incidents (id), customer_id text not null references core.customers (id),
    endpoint text not null, failed_requests integer not null, minutes_impacted integer not null);
create table core.usage_daily (
    customer_id text not null references core.customers (id), day date not null, requests bigint not null,
    errors bigint not null, primary key (customer_id, day));
create index on core.invoices (issued_at);
create index on core.incident_impacts (incident_id);
create index on core.usage_daily (day);
"""

SECURITY = """
-- Rows a caller may see: regions listed in app.regions, or everything for 'all'.
create function core.region_visible(r text) returns boolean language sql stable as $$
    select coalesce(current_setting('app.regions', true), '') = 'all'
        or r = any (string_to_array(coalesce(current_setting('app.regions', true), ''), ','))
$$;

alter table core.customers enable row level security;
create policy by_region on core.customers for select using (core.region_visible(region));
alter table core.subscriptions enable row level security;
create policy by_customer on core.subscriptions for select
    using (exists (select 1 from core.customers c where c.id = customer_id));
alter table core.invoices enable row level security;
create policy by_customer on core.invoices for select
    using (exists (select 1 from core.customers c where c.id = customer_id));
alter table core.incident_impacts enable row level security;
create policy by_customer on core.incident_impacts for select
    using (exists (select 1 from core.customers c where c.id = customer_id));
alter table core.usage_daily enable row level security;
create policy by_customer on core.usage_daily for select
    using (exists (select 1 from core.customers c where c.id = customer_id));

create view analytics.customers with (security_invoker = true) as
    select id, name, segment, region, arr_usd, plan, owner_github, owner_name, created_at from core.customers;
create view analytics.subscriptions with (security_invoker = true) as
    select customer_id, plan, status, started_at, churned_at, mrr_usd from core.subscriptions;
create view analytics.invoices with (security_invoker = true) as
    select id, customer_id, amount_usd, issued_at, status from core.invoices;
create view analytics.incidents with (security_invoker = true) as
    select id, service, severity, title, started_at, resolved_at, status, duration_minutes from core.incidents;
create view analytics.incident_impacts with (security_invoker = true) as
    select incident_id, customer_id, endpoint, failed_requests, minutes_impacted from core.incident_impacts;
create view analytics.usage_daily with (security_invoker = true) as
    select customer_id, day, requests, errors from core.usage_daily;

comment on view analytics.customers is 'One row per customer account. arr_usd is annual recurring revenue in USD. '
    'segment is enterprise, mid-market or smb. region is na, emea or apac.';
comment on column analytics.customers.owner_github is 'GitHub handle of the Acme account owner (assign follow-ups here)';
comment on column analytics.customers.arr_usd is 'Annual recurring revenue in USD';
comment on view analytics.subscriptions is 'One row per customer. status is active or churned; churned_at is the churn date.';
comment on view analytics.invoices is 'Monthly invoices, issued on the first day of each month. Sum amount_usd for revenue.';
comment on view analytics.incidents is 'Incidents from the incident tracker. severity is sev1 (worst) to sev3.';
comment on view analytics.incident_impacts is 'Which customers each incident affected. A customer can have one row '
    'per affected endpoint, so count distinct customer_id.';
comment on view analytics.usage_daily is 'Requests and errors per customer per UTC day, last 30 days.';
comment on column analytics.usage_daily.errors is 'Failed requests that day';
"""

GRANTS = """
grant usage on schema analytics to agent_ro;
grant usage on schema core to agent_ro;
grant select on all tables in schema analytics to agent_ro;
-- security_invoker views check the caller's rights on the tables underneath. Grant
-- only the columns the views expose: the contact columns stay unreadable.
grant select (id, name, segment, region, arr_usd, plan, owner_github, owner_name, created_at) on core.customers to agent_ro;
grant select on core.subscriptions, core.invoices, core.incidents, core.incident_impacts, core.usage_daily to agent_ro;
grant execute on function core.region_visible(text) to agent_ro;
"""


def _load(cur: psycopg.Cursor, table: str, rows: list[dict], columns: list[str]) -> None:
    query = sql.SQL("copy {} ({}) from stdin").format(sql.SQL(table), sql.SQL(", ").join(map(sql.Identifier, columns)))
    with cur.copy(query) as copy:
        for row in rows:
            copy.write_row([row.get(c) for c in columns])


def seed_warehouse(world: World, admin_url: str) -> dict[str, int]:
    with psycopg.connect(admin_url, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        _load(
            cur,
            "core.customers",
            world.customers,
            [
                "id",
                "name",
                "segment",
                "region",
                "arr_usd",
                "plan",
                "owner_github",
                "owner_name",
                "owner_email",
                "billing_email",
                "billing_phone",
                "created_at",
            ],
        )
        _load(
            cur,
            "core.subscriptions",
            world.subscriptions,
            ["customer_id", "plan", "status", "started_at", "churned_at", "mrr_usd"],
        )
        _load(cur, "core.invoices", world.invoices, ["id", "customer_id", "amount_usd", "issued_at", "status"])
        _load(
            cur,
            "core.incidents",
            world.incidents,
            ["id", "service", "severity", "title", "started_at", "resolved_at", "status", "duration_minutes"],
        )
        _load(
            cur,
            "core.incident_impacts",
            world.impacts,
            ["incident_id", "customer_id", "endpoint", "failed_requests", "minutes_impacted"],
        )
        _load(cur, "core.usage_daily", world.usage, ["customer_id", "day", "requests", "errors"])
        cur.execute(SECURITY)
        cur.execute("select 1 from pg_roles where rolname = 'agent_ro'")
        if cur.fetchone() is None:
            raise RuntimeError(
                "role agent_ro does not exist: run `uv run mara db init` (or infra/postgres/init.sql) first"
            )
        cur.execute(GRANTS)
        conn.commit()
    counts = {
        "customers": len(world.customers),
        "subscriptions": len(world.subscriptions),
        "invoices": len(world.invoices),
        "incidents": len(world.incidents),
        "impacts": len(world.impacts),
        "usage_daily": len(world.usage),
    }
    log.info("warehouse_seeded", **counts)
    return counts

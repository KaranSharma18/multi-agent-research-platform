"""Parse SQL and refuse anything that is not one safe SELECT over `analytics`.

The model sees the refusal reason as a tool error and can correct the query,
which is why the messages say what to do instead.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

ALLOWED_SCHEMA = "analytics"
MAX_SQL_CHARS = 4000

# Functions a SELECT could use to sleep, read files, change session settings
# (which would sidestep row-level security) or reach other databases.
DENIED_FUNCTIONS = {
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_get",
    "set_config",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "dblink",
    "dblink_exec",
    "query_to_xml",
    "pg_advisory_lock",
    "txid_current",
    "pg_notify",
    "current_setting",
}


class SQLRejected(ValueError):
    pass


def check_sql(sql: str, known_views: set[str]) -> str:
    """Return the normalised SQL if it is allowed, else raise SQLRejected."""
    if len(sql) > MAX_SQL_CHARS:
        raise SQLRejected(f"query is longer than {MAX_SQL_CHARS} characters; simplify it")
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as exc:
        raise SQLRejected(f"could not parse SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SQLRejected("send exactly one statement")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With)) and not (
        isinstance(stmt, exp.Subquery)
    ):
        raise SQLRejected("only SELECT (or WITH ... SELECT) is allowed")
    for node in stmt.walk():
        if isinstance(
            node,
            (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Drop,
                exp.Create,
                exp.Alter,
                exp.Command,
                exp.Merge,
                exp.TruncateTable,
                exp.Into,
                exp.Lock,
            ),
        ):
            raise SQLRejected(f"{node.key.upper()} is not allowed; this tool is read-only")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name in DENIED_FUNCTIONS or name.startswith("pg_") or name.startswith("lo_"):
                raise SQLRejected(f"function {name}() is not allowed")

    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    for table in stmt.find_all(exp.Table):
        name = table.name.lower()
        schema = (table.db or "").lower()
        if not schema and name in cte_names:
            continue
        if schema and schema != ALLOWED_SCHEMA:
            raise SQLRejected(f"table {schema}.{name} is outside the analytics schema")
        if name not in known_views:
            raise SQLRejected(f"unknown table {name}; call list_tables to see what exists")
    return stmt.sql(dialect="postgres")

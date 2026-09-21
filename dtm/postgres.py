"""
PostgreSQL backend for the Database Time Machine.

This mirrors the SQLite engine (``dtm.core``) but targets a real, multi-user
PostgreSQL server. The important difference: PostgreSQL *has* authenticated
users, so attribution no longer has to be cooperative -- the trigger records
``current_user`` automatically, and an optional session variable
(``dtm.author`` / ``dtm.message``) lets an application add a human name and a
reason on top.

    Status: this module is COMPLETE but requires a running PostgreSQL server and
    the ``psycopg`` driver (``pip install "database-time-machine[postgres]"``).
    It is therefore NOT exercised by the test suite, which stays dependency-free.
    The SQLite engine remains the reference, fully-tested implementation.

Design parity with the SQLite engine
------------------------------------
* ``_dtm_changes`` -- append-only JSONB change log (old_row / new_row + who/why).
* One trigger function, attached to every tracked table as AFTER
  INSERT/UPDATE/DELETE, writing into ``_dtm_changes``.
* ``as_of`` reconstructs a table at a point in time by taking, per primary key,
  the most recent change at or before that time.

Usage
-----
    from dtm.postgres import PostgresTimeMachine
    tm = PostgresTimeMachine("postgresql://user:pass@localhost/mydb")
    tm.init_repo()
    tm.track("public.users")
    tm.exec_sql("UPDATE users SET plan='pro' WHERE id=1",
                author="alice", message="upgrade")
    print(tm.as_of("public.users", "2026-09-19T00:00:00Z"))
"""

from __future__ import annotations

import json
from typing import Any

try:  # the driver is an optional extra; import lazily so core stays dependency-free
    import psycopg
except Exception:  # pragma: no cover - only relevant when the extra is installed
    psycopg = None


TRIGGER_FN = """
CREATE SCHEMA IF NOT EXISTS dtm;

CREATE TABLE IF NOT EXISTS dtm.changes (
    change_id  BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    tbl        TEXT,
    pk         TEXT,            -- primary-key value(s) as text
    op         TEXT,            -- INSERT | UPDATE | DELETE
    old_row    JSONB,
    new_row    JSONB,
    db_user    TEXT,            -- current_user: the REAL authenticated role
    author     TEXT,            -- optional application-supplied name
    message    TEXT
);
CREATE INDEX IF NOT EXISTS dtm_changes_lookup ON dtm.changes (tbl, pk, change_id);

CREATE OR REPLACE FUNCTION dtm.capture() RETURNS trigger AS $$
DECLARE
    k TEXT;
BEGIN
    -- primary key value, read from the trigger argument (column name)
    IF (TG_OP = 'DELETE') THEN
        EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO k USING OLD;
        INSERT INTO dtm.changes(tbl, pk, op, old_row, new_row, db_user, author, message)
        VALUES (TG_TABLE_SCHEMA||'.'||TG_TABLE_NAME, k, 'DELETE',
                to_jsonb(OLD), NULL, current_user,
                current_setting('dtm.author', true), current_setting('dtm.message', true));
        RETURN OLD;
    ELSE
        EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO k USING NEW;
        INSERT INTO dtm.changes(tbl, pk, op, old_row, new_row, db_user, author, message)
        VALUES (TG_TABLE_SCHEMA||'.'||TG_TABLE_NAME, k, TG_OP,
                CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) END,
                to_jsonb(NEW), current_user,
                current_setting('dtm.author', true), current_setting('dtm.message', true));
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;
"""


class PostgresTimeMachine:
    def __init__(self, dsn: str):
        if psycopg is None:
            raise RuntimeError(
                "The PostgreSQL backend needs psycopg. Install it with:\n"
                '    pip install "database-time-machine[postgres]"'
            )
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, autocommit=True)

    # ------------------------------------------------------------------ #
    def init_repo(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(TRIGGER_FN)

    def _pk_column(self, qualified: str) -> str:
        """Return the primary-key column of a schema-qualified table."""
        schema, _, table = qualified.partition(".")
        schema = schema or "public"
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                WHERE i.indrelid = %s::regclass AND i.indisprimary
                LIMIT 1
                """,
                (f"{schema}.{table}",),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError(f"{qualified} has no primary key to track by")
        return row[0]

    def track(self, qualified: str) -> None:
        """Attach the capture trigger to a table (schema-qualified, e.g.
        'public.users')."""
        pk = self._pk_column(qualified)
        schema, _, table = qualified.partition(".")
        schema = schema or "public"
        trig = f"dtm_capture_{schema}_{table}"
        with self.conn.cursor() as cur:
            cur.execute(f'DROP TRIGGER IF EXISTS "{trig}" ON "{schema}"."{table}"')
            cur.execute(
                f'CREATE TRIGGER "{trig}" AFTER INSERT OR UPDATE OR DELETE '
                f'ON "{schema}"."{table}" FOR EACH ROW '
                f"EXECUTE FUNCTION dtm.capture(%s)" % f"'{pk}'"
            )

    # ------------------------------------------------------------------ #
    def exec_sql(self, sql: str, author: str = "", message: str = "") -> None:
        """Run attributed SQL. ``author``/``message`` are exposed to the trigger
        through session settings; ``current_user`` is always recorded too."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT set_config('dtm.author', %s, false)", (author,))
            cur.execute("SELECT set_config('dtm.message', %s, false)", (message,))
            cur.execute(sql)

    def log(self, table: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM dtm.changes"
        params: list[Any] = []
        if table:
            q += " WHERE tbl = %s"
            params.append(table)
        q += " ORDER BY change_id DESC LIMIT %s"
        params.append(limit)
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def as_of(self, table: str, at: str) -> list[dict[str, Any]]:
        """Reconstruct `table` as of timestamp `at` (ISO-8601)."""
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                """
                SELECT c.pk, c.op, c.new_row
                FROM dtm.changes c
                WHERE c.tbl = %s AND c.ts <= %s
                  AND c.change_id = (
                      SELECT max(c2.change_id) FROM dtm.changes c2
                      WHERE c2.tbl=c.tbl AND c2.pk=c.pk AND c2.ts <= %s)
                """,
                (table, at, at),
            )
            rows = cur.fetchall()
        return [r["new_row"] for r in rows if r["op"] != "DELETE"]

    def blame(self, table: str, pk: str, column: str) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT * FROM dtm.changes WHERE tbl=%s AND pk=%s "
                "ORDER BY change_id DESC",
                (table, str(pk)),
            )
            for r in cur.fetchall():
                new = (r["new_row"] or {}).get(column)
                old = (r["old_row"] or {}).get(column)
                if r["op"] == "DELETE" or new != old:
                    return {
                        "change_id": r["change_id"], "ts": r["ts"],
                        "db_user": r["db_user"], "author": r["author"],
                        "message": r["message"], "op": r["op"],
                        "old_value": old, "new_value": None if r["op"] == "DELETE" else new,
                    }
        return None

    def close(self) -> None:
        self.conn.close()

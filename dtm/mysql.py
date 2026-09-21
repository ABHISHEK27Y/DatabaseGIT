"""
MySQL / MariaDB backend for the Database Time Machine.

Mirrors the SQLite engine (``dtm.core``) on a real MySQL server. Like the
PostgreSQL backend, attribution can come from the authenticated session
(``CURRENT_USER()``) plus an optional application-supplied author/message
carried in session user variables (``@dtm_author`` / ``@dtm_message``).

    Status: COMPLETE but requires a running MySQL/MariaDB server and a driver
    (``pip install "database-time-machine[mysql]"`` -> PyMySQL). It is therefore
    NOT exercised by the dependency-free test suite; the SQLite engine remains
    the fully-tested reference implementation.

Design parity with the SQLite engine
------------------------------------
* ``dtm_changes`` -- append-only JSON change log (old_row / new_row + who/why).
* One AFTER INSERT/UPDATE/DELETE trigger per tracked table writes into it,
  keyed by the table's primary key.
* ``as_of`` reconstructs a table at a point in time from the most recent change
  per key at or before that time.

Usage
-----
    from dtm.mysql import MySQLTimeMachine
    tm = MySQLTimeMachine(host="localhost", user="root", password="…", database="shop")
    tm.init_repo()
    tm.track("products")
    tm.exec_sql("UPDATE products SET price=9.99 WHERE id=1",
                author="alice", message="price fix")
    print(tm.as_of("products", "2026-09-19 00:00:00"))
"""

from __future__ import annotations

from typing import Any

try:  # optional driver; keeps the core dependency-free
    import pymysql
except Exception:  # pragma: no cover
    pymysql = None


SETUP = """
CREATE TABLE IF NOT EXISTS dtm_changes (
    change_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    tbl       VARCHAR(255),
    pk        VARCHAR(255),
    op        VARCHAR(8),
    old_row   JSON,
    new_row   JSON,
    db_user   VARCHAR(255),
    author    VARCHAR(255),
    message   TEXT,
    INDEX dtm_changes_lookup (tbl, pk, change_id)
);
"""


def _triggers(table: str, pk: str) -> list[str]:
    """AFTER INSERT/UPDATE/DELETE triggers for one table (MySQL has no single
    multi-event trigger, so we create three)."""
    common = (
        "db_user, author, message) VALUES ("
        "'{tbl}', CAST({row}.`{pk}` AS CHAR), '{op}', {old}, {new}, "
        "CURRENT_USER(), @dtm_author, @dtm_message)"
    )
    ins = f"""
    CREATE TRIGGER `dtm_{table}_ins` AFTER INSERT ON `{table}` FOR EACH ROW
    INSERT INTO dtm_changes (tbl, pk, op, old_row, new_row, """ + common.format(
        tbl=table, row="NEW", pk=pk, op="INSERT", old="NULL",
        new=f"JSON_OBJECT{_json_args(table)}") + ";"
    upd = f"""
    CREATE TRIGGER `dtm_{table}_upd` AFTER UPDATE ON `{table}` FOR EACH ROW
    INSERT INTO dtm_changes (tbl, pk, op, old_row, new_row, """ + common.format(
        tbl=table, row="NEW", pk=pk, op="UPDATE",
        old=f"JSON_OBJECT{_json_args(table, 'OLD')}",
        new=f"JSON_OBJECT{_json_args(table, 'NEW')}") + ";"
    dele = f"""
    CREATE TRIGGER `dtm_{table}_del` AFTER DELETE ON `{table}` FOR EACH ROW
    INSERT INTO dtm_changes (tbl, pk, op, old_row, new_row, """ + common.format(
        tbl=table, row="OLD", pk=pk, op="DELETE",
        old=f"JSON_OBJECT{_json_args(table, 'OLD')}", new="NULL") + ";"
    return [ins, upd, dele]


# NOTE: column list is filled in per table at track() time (see below); this
# placeholder keeps the trigger text readable.
_COLUMNS: dict[str, list[str]] = {}


def _json_args(table: str, alias: str = "NEW") -> str:
    cols = _COLUMNS.get(table, [])
    inner = ", ".join(f"'{c}', {alias}.`{c}`" for c in cols)
    return f"({inner})"


class MySQLTimeMachine:
    def __init__(self, **connect_kwargs: Any):
        if pymysql is None:
            raise RuntimeError(
                "The MySQL backend needs PyMySQL. Install it with:\n"
                '    pip install "database-time-machine[mysql]"'
            )
        self.conn = pymysql.connect(autocommit=True, **connect_kwargs)

    def init_repo(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SETUP)

    def _pk_column(self, table: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                "AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION LIMIT 1",
                (table,),
            )
            row = cur.fetchone()
        if not row:
            raise RuntimeError(f"{table} has no primary key to track by")
        return row[0]

    def _column_names(self, table: str) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s "
                "ORDER BY ORDINAL_POSITION",
                (table,),
            )
            return [r[0] for r in cur.fetchall()]

    def track(self, table: str) -> None:
        pk = self._pk_column(table)
        _COLUMNS[table] = self._column_names(table)
        with self.conn.cursor() as cur:
            for suffix in ("ins", "upd", "del"):
                cur.execute(f"DROP TRIGGER IF EXISTS `dtm_{table}_{suffix}`")
            for stmt in _triggers(table, pk):
                cur.execute(stmt)

    def exec_sql(self, sql: str, author: str = "", message: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute("SET @dtm_author=%s, @dtm_message=%s", (author, message))
            cur.execute(sql)

    def log(self, table: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM dtm_changes"
        params: list[Any] = []
        if table:
            q += " WHERE tbl=%s"
            params.append(table)
        q += " ORDER BY change_id DESC LIMIT %s"
        params.append(limit)
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(q, params)
            return cur.fetchall()

    def as_of(self, table: str, at: str) -> list[dict[str, Any]]:
        import json as _json
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                """
                SELECT c.pk, c.op, c.new_row FROM dtm_changes c
                WHERE c.tbl=%s AND c.ts <= %s
                  AND c.change_id = (
                      SELECT MAX(c2.change_id) FROM dtm_changes c2
                      WHERE c2.tbl=c.tbl AND c2.pk=c.pk AND c2.ts <= %s)
                """,
                (table, at, at),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            if r["op"] == "DELETE":
                continue
            v = r["new_row"]
            out.append(_json.loads(v) if isinstance(v, str) else v)
        return out

    def close(self) -> None:
        self.conn.close()

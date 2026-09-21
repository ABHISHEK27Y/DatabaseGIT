"""
Database Time Machine -- core engine.

"Git for databases": every schema and data change to a SQLite database is
captured into an append-only log with author + message + timestamp, so you can
ask questions like:

    * Who changed this column, and why?
    * What did this table look like on August 20?
    * Show me every change to this row over time.

The engine works by installing AFTER INSERT/UPDATE/DELETE triggers on every user
table. Each trigger writes a row into ``_dtm_changes`` recording the old and new
row as JSON, plus the author/message/timestamp of the current transaction (read
from a small ``_dtm_context`` table that the CLI sets before running any SQL).

Only the Python standard library is used (sqlite3, json, datetime).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

# Prefixes / names reserved by the engine. User tables never start with "_dtm_".
META_PREFIX = "_dtm_"
INTERNAL_TABLES = {"sqlite_sequence"}


def _now() -> str:
    """UTC timestamp, ISO-8601, microsecond precision.

    Lexicographic string ordering of this format matches chronological order,
    which is what the time-travel queries rely on.
    """
    return datetime.now(timezone.utc).isoformat()


class TimeMachineError(Exception):
    pass


class TimeMachine:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL + synchronous=NORMAL is the standard "fast but durable" combo:
        # it turns per-row trigger writes from ~10s to ~0.2s for a 2000-change
        # batch, without the corruption risk of synchronous=OFF.
        try:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.OperationalError:  # e.g. a read-only or network filesystem
            pass
        self._check_json1()

    # ------------------------------------------------------------------ #
    # setup
    # ------------------------------------------------------------------ #
    def _check_json1(self) -> None:
        try:
            self.conn.execute("SELECT json_object('a', 1)").fetchone()
        except sqlite3.OperationalError as exc:  # pragma: no cover
            raise TimeMachineError(
                "This SQLite build lacks the JSON1 extension, which the Time "
                "Machine needs. Use Python 3.9+ with a modern SQLite."
            ) from exc

    def init_repo(self) -> None:
        """Create the engine's metadata tables and start tracking everything."""
        c = self.conn
        c.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {META_PREFIX}meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            -- Single-row scratch table the CLI writes before running user SQL,
            -- so triggers can attribute the change to an author/message/txn.
            CREATE TABLE IF NOT EXISTS {META_PREFIX}context (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                txn_id  INTEGER,
                author  TEXT,
                message TEXT,
                ts      TEXT
            );

            -- One row per exec() call: a "commit".
            CREATE TABLE IF NOT EXISTS {META_PREFIX}txn (
                txn_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT,
                author  TEXT,
                message TEXT
            );

            -- Append-only row-level change log. This is the heart of the tool.
            -- row_hash/prev_hash form a tamper-evident chain (see _hash_new_changes).
            CREATE TABLE IF NOT EXISTS {META_PREFIX}changes (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_id    INTEGER,
                ts        TEXT,
                tbl       TEXT,
                pk        INTEGER,      -- rowid of the affected row
                op        TEXT,         -- INSERT | UPDATE | DELETE
                old_json  TEXT,
                new_json  TEXT,
                author    TEXT,
                message   TEXT,
                prev_hash TEXT,
                row_hash  TEXT
            );
            CREATE INDEX IF NOT EXISTS {META_PREFIX}changes_lookup
                ON {META_PREFIX}changes (tbl, pk, change_id);

            -- Which user tables are tracked, and since when.
            CREATE TABLE IF NOT EXISTS {META_PREFIX}tracked (
                tbl   TEXT PRIMARY KEY,
                since TEXT
            );

            -- Schema snapshots, one per detected schema change.
            CREATE TABLE IF NOT EXISTS {META_PREFIX}schema (
                schema_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_id      INTEGER,
                ts          TEXT,
                author      TEXT,
                message     TEXT,
                schema_json TEXT      -- {{table: [{{name,type,notnull,dflt,pk}}]}}
            );

            -- Named checkpoints, so you can time-travel to a name (like a git tag).
            CREATE TABLE IF NOT EXISTS {META_PREFIX}tags (
                name       TEXT PRIMARY KEY,
                ts         TEXT,      -- the point in time this tag marks
                author     TEXT,
                message    TEXT,
                created_ts TEXT
            );

            -- Branches forked from this database (git-style branching).
            CREATE TABLE IF NOT EXISTS {META_PREFIX}branches (
                name            TEXT PRIMARY KEY,
                path            TEXT,   -- the forked database file
                created_from_ts TEXT,   -- the point in time it diverged
                created_ts      TEXT,
                author          TEXT
            );
            """
        )
        self._migrate()
        c.execute(
            f"INSERT OR IGNORE INTO {META_PREFIX}meta(key, value) VALUES ('version', '2')"
        )
        c.commit()
        self._set_context("system", "dtm init", txn_id=None)
        self.resync_tracking()
        self._maybe_snapshot_schema(txn_id=None, author="system", message="dtm init")
        self._hash_new_changes()
        c.commit()

    def _migrate(self) -> None:
        """Add columns introduced by later versions to an older repo, in place."""
        cols = {r["name"] for r in self.conn.execute(
            f'PRAGMA table_info("{META_PREFIX}changes")')}
        for col in ("prev_hash", "row_hash"):
            if col not in cols:
                self.conn.execute(
                    f"ALTER TABLE {META_PREFIX}changes ADD COLUMN {col} TEXT")

    # ------------------------------------------------------------------ #
    # introspection helpers
    # ------------------------------------------------------------------ #
    def user_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        out = []
        for r in rows:
            name = r["name"]
            if name.startswith(META_PREFIX) or name in INTERNAL_TABLES:
                continue
            out.append(name)
        return out

    def columns(self, table: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
        return [
            {
                "name": r["name"],
                "type": r["type"],
                "notnull": r["notnull"],
                "dflt": r["dflt_value"],
                "pk": r["pk"],
            }
            for r in rows
        ]

    def _quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    # ------------------------------------------------------------------ #
    # tracking / triggers
    # ------------------------------------------------------------------ #
    def _str_literal(self, s: str) -> str:
        """A safely-quoted SQL string literal (single quotes doubled)."""
        return "'" + s.replace("'", "''") + "'"

    def _trigger_name(self, table: str, suffix: str) -> str:
        """A valid, unique, deterministic trigger identifier for any table name
        (handles spaces, quotes, unicode, collisions)."""
        safe = re.sub(r"\W", "_", table)[:40]
        digest = hashlib.md5(table.encode("utf-8")).hexdigest()[:8]
        return f'"{META_PREFIX}{safe}_{digest}_{suffix}"'

    def _has_rowid(self, table: str) -> bool:
        """False for WITHOUT ROWID tables (which have no `rowid` to track by)."""
        try:
            self.conn.execute(f'SELECT rowid FROM {self._quote_ident(table)} LIMIT 0')
            return True
        except sqlite3.OperationalError:
            return False

    def _json_object_expr(self, table: str, alias: str) -> str:
        """Build a json_object(...) expression over all columns of `table`,
        reading from the NEW/OLD trigger alias. Column names are safely quoted."""
        parts = []
        for col in self.columns(table):
            name = col["name"]
            parts.append(f"{self._str_literal(name)}, {alias}.{self._quote_ident(name)}")
        return f"json_object({', '.join(parts)})" if parts else "json_object()"

    def _install_triggers(self, table: str) -> None:
        c = self.conn
        qt = self._quote_ident(table)
        tbl_lit = self._str_literal(table)
        for suffix in ("ins", "upd", "del"):
            c.execute(f"DROP TRIGGER IF EXISTS {self._trigger_name(table, suffix)}")

        new_obj = self._json_object_expr(table, "NEW")
        old_obj = self._json_object_expr(table, "OLD")
        ctx = f"(SELECT %s FROM {META_PREFIX}context WHERE id=1)"

        c.execute(
            f"""
            CREATE TRIGGER {self._trigger_name(table, 'ins')} AFTER INSERT ON {qt}
            BEGIN
                INSERT INTO {META_PREFIX}changes
                    (txn_id, ts, tbl, pk, op, old_json, new_json, author, message)
                VALUES ({ctx % 'txn_id'}, {ctx % 'ts'}, {tbl_lit}, NEW.rowid,
                        'INSERT', NULL, {new_obj}, {ctx % 'author'}, {ctx % 'message'});
            END;
            """
        )
        c.execute(
            f"""
            CREATE TRIGGER {self._trigger_name(table, 'upd')} AFTER UPDATE ON {qt}
            BEGIN
                INSERT INTO {META_PREFIX}changes
                    (txn_id, ts, tbl, pk, op, old_json, new_json, author, message)
                VALUES ({ctx % 'txn_id'}, {ctx % 'ts'}, {tbl_lit}, NEW.rowid,
                        'UPDATE', {old_obj}, {new_obj}, {ctx % 'author'}, {ctx % 'message'});
            END;
            """
        )
        c.execute(
            f"""
            CREATE TRIGGER {self._trigger_name(table, 'del')} AFTER DELETE ON {qt}
            BEGIN
                INSERT INTO {META_PREFIX}changes
                    (txn_id, ts, tbl, pk, op, old_json, new_json, author, message)
                VALUES ({ctx % 'txn_id'}, {ctx % 'ts'}, {tbl_lit}, OLD.rowid,
                        'DELETE', {old_obj}, NULL, {ctx % 'author'}, {ctx % 'message'});
            END;
            """
        )

    def _baseline_table(self, table: str) -> None:
        """Record existing rows of a newly tracked table as synthetic INSERTs,
        so time-travel has a creation event for pre-existing data."""
        qt = self._quote_ident(table)
        obj = self._json_object_expr(table, "t")
        # Use the current context (author/ts/txn) for the baseline rows.
        self.conn.execute(
            f"""
            INSERT INTO {META_PREFIX}changes
                (txn_id, ts, tbl, pk, op, old_json, new_json, author, message)
            SELECT
                (SELECT txn_id FROM {META_PREFIX}context WHERE id=1),
                (SELECT ts FROM {META_PREFIX}context WHERE id=1),
                {self._str_literal(table)}, t.rowid, 'INSERT', NULL, {obj},
                (SELECT author FROM {META_PREFIX}context WHERE id=1),
                'baseline snapshot'
            FROM {qt} t
            """
        )

    def resync_tracking(self) -> None:
        """Install triggers on any user table not yet tracked (and baseline it).
        Also refreshes triggers so column changes are picked up.

        WITHOUT ROWID tables have no `rowid` to key history by, so they are
        skipped gracefully (recorded in _dtm_meta) rather than crashing.
        """
        tracked = {
            r["tbl"]
            for r in self.conn.execute(f"SELECT tbl FROM {META_PREFIX}tracked")
        }
        skipped = []
        for table in self.user_tables():
            if not self._has_rowid(table):
                skipped.append(table)
                continue
            first_time = table not in tracked
            self._install_triggers(table)
            if first_time:
                self._baseline_table(table)
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {META_PREFIX}tracked(tbl, since) "
                    f"VALUES (?, (SELECT ts FROM {META_PREFIX}context WHERE id=1))",
                    (table,),
                )
        if skipped:
            self.conn.execute(
                f"INSERT OR REPLACE INTO {META_PREFIX}meta(key, value) VALUES "
                f"('untracked_without_rowid', ?)",
                (json.dumps(skipped),),
            )

    # ------------------------------------------------------------------ #
    # context / transactions
    # ------------------------------------------------------------------ #
    def _new_txn(self, author: str, message: str, ts: str) -> int:
        cur = self.conn.execute(
            f"INSERT INTO {META_PREFIX}txn(ts, author, message) VALUES (?,?,?)",
            (ts, author, message),
        )
        return int(cur.lastrowid)

    def _set_context(
        self, author: str, message: str, txn_id: int | None, ts: str | None = None
    ) -> str:
        ts = ts or _now()
        self.conn.execute(
            f"INSERT OR REPLACE INTO {META_PREFIX}context(id, txn_id, author, message, ts) "
            f"VALUES (1, ?, ?, ?, ?)",
            (txn_id, author, message, ts),
        )
        return ts

    # ------------------------------------------------------------------ #
    # schema snapshots
    # ------------------------------------------------------------------ #
    def _current_schema(self) -> dict[str, list[dict[str, Any]]]:
        return {t: self.columns(t) for t in self.user_tables()}

    def _latest_schema(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT schema_json FROM {META_PREFIX}schema ORDER BY schema_id DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["schema_json"]) if row else None

    def _maybe_snapshot_schema(
        self, txn_id: int | None, author: str, message: str
    ) -> bool:
        current = self._current_schema()
        if current == self._latest_schema():
            return False
        self.conn.execute(
            f"INSERT INTO {META_PREFIX}schema(txn_id, ts, author, message, schema_json) "
            f"VALUES (?, ?, ?, ?, ?)",
            (txn_id, _now(), author, message, json.dumps(current)),
        )
        return True

    # ------------------------------------------------------------------ #
    # the main entry point for changing data
    # ------------------------------------------------------------------ #
    def _run(self, author: str, message: str, body) -> int:
        """Run `body(txn_id)` inside one attributed transaction.

        Sets up the txn + context (so triggers can attribute the writes), runs
        the body, then resyncs tracking, snapshots schema if it changed, and
        commits. Any SQL error rolls the whole thing back. Returns the txn id.
        """
        ts = _now()
        txn_id = self._new_txn(author, message, ts)
        self._set_context(author, message, txn_id, ts=ts)
        try:
            body(txn_id)
        except sqlite3.Error as exc:
            self.conn.rollback()
            raise TimeMachineError(f"SQL failed: {exc}") from exc
        # New tables created by this statement need triggers; existing tables
        # whose schema changed need refreshed triggers + a schema snapshot.
        self.resync_tracking()
        self._maybe_snapshot_schema(txn_id, author, message)
        self._hash_new_changes()
        self.conn.commit()
        return txn_id

    def exec_sql(self, sql: str, author: str, message: str) -> None:
        """Run user SQL (DDL and/or DML) as a single attributed transaction."""
        self._run(author, message, lambda txn_id: self.conn.executescript(sql))

    @contextmanager
    def session(self, author: str, message: str = ""):
        """Programmatic API: run several statements as one attributed commit.

            with tm.session(author="alice", message="import") as cur:
                cur.execute("INSERT INTO products(name) VALUES('X')")
                cur.execute("UPDATE products SET price=9 WHERE name='X'")

        Everything inside the block is captured under one transaction, attributed
        to `author`, and committed on clean exit (rolled back on exception).
        """
        ts = _now()
        txn_id = self._new_txn(author, message, ts)
        self._set_context(author, message, txn_id, ts=ts)
        cur = self.conn.cursor()
        try:
            yield cur
        except Exception:
            self.conn.rollback()
            raise
        self.resync_tracking()
        self._maybe_snapshot_schema(txn_id, author, message)
        self._hash_new_changes()
        self.conn.commit()

    def query(self, sql: str) -> list[sqlite3.Row]:
        """Read-only query against the live database."""
        return self.conn.execute(sql).fetchall()

    # ------------------------------------------------------------------ #
    # time-travel & history
    # ------------------------------------------------------------------ #
    def as_of(self, table: str, at: str) -> list[dict[str, Any]]:
        """Reconstruct the state of `table` as it was at timestamp `at`.

        For each row (keyed by rowid) we take its most recent change at or
        before `at`; if that change was a DELETE the row is gone, otherwise the
        post-change JSON is the row's state.
        """
        rows = self.conn.execute(
            f"""
            SELECT c.pk, c.op, c.new_json
            FROM {META_PREFIX}changes c
            WHERE c.tbl = ? AND c.ts <= ?
              AND c.change_id = (
                  SELECT c2.change_id FROM {META_PREFIX}changes c2
                  WHERE c2.tbl = c.tbl AND c2.pk = c.pk AND c2.ts <= ?
                  ORDER BY c2.ts DESC, c2.change_id DESC LIMIT 1
              )
            ORDER BY c.pk
            """,
            (table, at, at),
        ).fetchall()
        out = []
        for r in rows:
            if r["op"] == "DELETE":
                continue
            out.append(json.loads(r["new_json"]))
        return out

    def row_history(self, table: str, pk: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"""
            SELECT change_id, ts, op, old_json, new_json, author, message
            FROM {META_PREFIX}changes
            WHERE tbl = ? AND pk = ?
            ORDER BY change_id
            """,
            (table, pk),
        ).fetchall()
        return [dict(r) for r in rows]

    def log(
        self,
        table: str | None = None,
        limit: int = 20,
        author: str | None = None,
        op: str | None = None,
        since: str | None = None,
        until: str | None = None,
        contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent changes, newest first, with optional filters.

        `since`/`until` are ISO timestamps (or tag names, resolved by the caller);
        `contains` matches a substring in the message or in the row values.
        """
        where, params = [], []
        if table:
            where.append("tbl = ?"); params.append(table)
        if author:
            where.append("author = ?"); params.append(author)
        if op:
            where.append("op = ?"); params.append(op.upper())
        if since:
            where.append("ts >= ?"); params.append(since)
        if until:
            where.append("ts <= ?"); params.append(until)
        if contains:
            where.append("(message LIKE ? OR new_json LIKE ? OR old_json LIKE ?)")
            params += [f"%{contains}%"] * 3
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM {META_PREFIX}changes {clause} "
            f"ORDER BY change_id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def blame(self, table: str, pk: int, column: str) -> dict[str, Any] | None:
        """Who last changed `column` of this row, when, why, and what changed."""
        rows = self.conn.execute(
            f"""
            SELECT change_id, ts, op, old_json, new_json, author, message
            FROM {META_PREFIX}changes
            WHERE tbl = ? AND pk = ?
            ORDER BY change_id DESC
            """,
            (table, pk),
        ).fetchall()
        for r in rows:
            new = json.loads(r["new_json"]) if r["new_json"] else {}
            old = json.loads(r["old_json"]) if r["old_json"] else {}
            new_val = new.get(column)
            old_val = old.get(column)
            if r["op"] == "DELETE":
                # the row (and column) was removed here
                return {
                    "change_id": r["change_id"],
                    "ts": r["ts"],
                    "author": r["author"],
                    "message": r["message"],
                    "op": r["op"],
                    "old_value": old_val,
                    "new_value": None,
                }
            if new_val != old_val:
                return {
                    "change_id": r["change_id"],
                    "ts": r["ts"],
                    "author": r["author"],
                    "message": r["message"],
                    "op": r["op"],
                    "old_value": old_val,
                    "new_value": new_val,
                }
        return None

    def diff(self, table: str, frm: str, to: str) -> dict[str, list[dict[str, Any]]]:
        """Diff `table` between two timestamps, keyed by rowid."""
        before = self._as_of_by_pk(table, frm)
        after = self._as_of_by_pk(table, to)
        added, removed, changed = [], [], []
        for pk, row in after.items():
            if pk not in before:
                added.append({"pk": pk, "row": row})
            elif before[pk] != row:
                fields = [
                    k for k in set(row) | set(before[pk])
                    if before[pk].get(k) != row.get(k)
                ]
                changed.append({
                    "pk": pk, "before": before[pk], "after": row, "fields": fields,
                })
        for pk, row in before.items():
            if pk not in after:
                removed.append({"pk": pk, "row": row})
        return {"added": added, "removed": removed, "changed": changed}

    def _as_of_by_pk(self, table: str, at: str) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute(
            f"""
            SELECT c.pk, c.op, c.new_json
            FROM {META_PREFIX}changes c
            WHERE c.tbl = ? AND c.ts <= ?
              AND c.change_id = (
                  SELECT c2.change_id FROM {META_PREFIX}changes c2
                  WHERE c2.tbl = c.tbl AND c2.pk = c.pk AND c2.ts <= ?
                  ORDER BY c2.ts DESC, c2.change_id DESC LIMIT 1
              )
            """,
            (table, at, at),
        ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            if r["op"] == "DELETE":
                continue
            out[r["pk"]] = json.loads(r["new_json"])
        return out

    # ------------------------------------------------------------------ #
    # schema history
    # ------------------------------------------------------------------ #
    def schema_log(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT schema_id, ts, author, message FROM {META_PREFIX}schema "
            f"ORDER BY schema_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def schema_blame(self, table: str, column: str) -> dict[str, Any] | None:
        """Find the schema snapshot in which this column last appeared/changed."""
        snaps = self.conn.execute(
            f"SELECT ts, author, message, schema_json FROM {META_PREFIX}schema "
            f"ORDER BY schema_id"
        ).fetchall()
        prev_col = None
        result = None
        for s in snaps:
            schema = json.loads(s["schema_json"])
            cols = {c["name"]: c for c in schema.get(table, [])}
            cur_col = cols.get(column)
            if cur_col != prev_col:  # appeared, changed type, or dropped
                result = {
                    "ts": s["ts"],
                    "author": s["author"],
                    "message": s["message"],
                    "definition": cur_col,
                }
            prev_col = cur_col
        return result

    # ------------------------------------------------------------------ #
    # tags / named checkpoints
    # ------------------------------------------------------------------ #
    def tag(
        self, name: str, author: str, message: str = "", at: str | None = None
    ) -> str:
        """Create (or move) a named checkpoint pointing at time `at`
        (defaults to now). Time-travel commands accept the name in place of a
        raw timestamp."""
        ts = at or _now()
        self.conn.execute(
            f"INSERT OR REPLACE INTO {META_PREFIX}tags(name, ts, author, message, created_ts) "
            f"VALUES (?, ?, ?, ?, ?)",
            (name, ts, author, message, _now()),
        )
        self.conn.commit()
        return ts

    def list_tags(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT name, ts, author, message, created_ts FROM {META_PREFIX}tags "
            f"ORDER BY ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_time(self, spec: str) -> str:
        """Turn a time spec into an ISO timestamp.

        Accepts a tag name, the literal 'now'/'HEAD', or an ISO timestamp
        (returned unchanged).
        """
        if spec in ("now", "HEAD", "head"):
            return _now()
        row = self.conn.execute(
            f"SELECT ts FROM {META_PREFIX}tags WHERE name = ?", (spec,)
        ).fetchone()
        if row:
            return row["ts"]
        return spec

    # ------------------------------------------------------------------ #
    # revert -- restore a table to a past state
    # ------------------------------------------------------------------ #
    def _integer_pk_alias(self, table: str) -> str | None:
        """Return the column that is an INTEGER PRIMARY KEY alias for rowid,
        if the table has exactly one such column (so we don't set rowid twice)."""
        cols = self.columns(table)
        pks = [c for c in cols if c["pk"]]
        if len(pks) == 1 and (pks[0]["type"] or "").upper() == "INTEGER":
            return pks[0]["name"]
        return None

    def _live_rows_by_pk(self, table: str) -> dict[int, dict[str, Any]]:
        qt = self._quote_ident(table)
        rows = self.conn.execute(f"SELECT rowid AS _rid, * FROM {qt}").fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            rid = d.pop("_rid")
            out[rid] = d
        return out

    def revert(
        self, table: str, at: str, author: str, message: str = ""
    ) -> dict[str, int]:
        """Restore `table` to exactly the state it had at time `at`.

        The revert is applied through the normal write path, so it is itself
        recorded as a set of INSERT/UPDATE/DELETE changes (you can revert a
        revert). Returns a summary count of the operations applied.
        """
        at = self.resolve_time(at)
        target = self._as_of_by_pk(table, at)      # {rowid: full row}
        cols = [c["name"] for c in self.columns(table)]
        alias = self._integer_pk_alias(table)
        summary = {"inserted": 0, "updated": 0, "deleted": 0}

        def body(txn_id: int) -> None:
            qt = self._quote_ident(table)
            current = self._live_rows_by_pk(table)

            # rows that exist now but should not -> delete
            for rid in current:
                if rid not in target:
                    self.conn.execute(f"DELETE FROM {qt} WHERE rowid = ?", (rid,))
                    summary["deleted"] += 1

            for rid, row in target.items():
                if rid not in current:
                    # re-create the row, preserving its rowid identity
                    if alias:
                        insert_cols = cols
                        values = [row.get(c) for c in cols]
                    else:
                        insert_cols = ["rowid"] + cols
                        values = [rid] + [row.get(c) for c in cols]
                    collist = ", ".join(self._quote_ident(c) for c in insert_cols)
                    ph = ", ".join(["?"] * len(insert_cols))
                    self.conn.execute(
                        f"INSERT INTO {qt} ({collist}) VALUES ({ph})", values
                    )
                    summary["inserted"] += 1
                elif current[rid] != row:
                    set_cols = [c for c in cols if c != alias]
                    assignments = ", ".join(
                        f"{self._quote_ident(c)} = ?" for c in set_cols
                    )
                    values = [row.get(c) for c in set_cols] + [rid]
                    self.conn.execute(
                        f"UPDATE {qt} SET {assignments} WHERE rowid = ?", values
                    )
                    summary["updated"] += 1

        msg = message or f"revert {table} to {at}"
        self._run(author, msg, body)
        return summary

    # ------------------------------------------------------------------ #
    # stats / dashboard data
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        c = self.conn

        def rows_to_list(sql):
            return [dict(r) for r in c.execute(sql).fetchall()]

        total = c.execute(
            f"SELECT COUNT(*) AS n FROM {META_PREFIX}changes"
        ).fetchone()["n"]
        by_op = {
            r["op"]: r["n"]
            for r in c.execute(
                f"SELECT op, COUNT(*) AS n FROM {META_PREFIX}changes GROUP BY op"
            )
        }
        by_author = rows_to_list(
            f"SELECT author, COUNT(*) AS n FROM {META_PREFIX}changes "
            f"GROUP BY author ORDER BY n DESC"
        )
        by_table = rows_to_list(
            f"SELECT tbl, COUNT(*) AS n FROM {META_PREFIX}changes "
            f"GROUP BY tbl ORDER BY n DESC"
        )
        by_day = rows_to_list(
            f"SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n "
            f"FROM {META_PREFIX}changes GROUP BY day ORDER BY day"
        )
        most_edited = rows_to_list(
            f"SELECT tbl, pk, COUNT(*) AS n FROM {META_PREFIX}changes "
            f"GROUP BY tbl, pk ORDER BY n DESC LIMIT 10"
        )
        span = c.execute(
            f"SELECT MIN(ts) AS first, MAX(ts) AS last FROM {META_PREFIX}changes"
        ).fetchone()
        txns = c.execute(
            f"SELECT COUNT(*) AS n FROM {META_PREFIX}txn"
        ).fetchone()["n"]
        return {
            "total_changes": total,
            "total_txns": txns,
            "tables_tracked": len(self.user_tables()),
            "tags": len(self.list_tags()),
            "by_op": by_op,
            "by_author": by_author,
            "by_table": by_table,
            "by_day": by_day,
            "most_edited_rows": most_edited,
            "first_change": span["first"],
            "last_change": span["last"],
        }

    # ------------------------------------------------------------------ #
    # tamper-evident hash chain
    # ------------------------------------------------------------------ #
    _HASH_FIELDS = ("change_id", "txn_id", "ts", "tbl", "pk", "op",
                    "old_json", "new_json", "author", "message")

    def _payload(self, r) -> str:
        return "|".join("" if r[k] is None else str(r[k]) for k in self._HASH_FIELDS)

    def _hash_new_changes(self) -> None:
        """Chain-hash any change rows that don't yet have a hash.

        Each row's hash = sha256(previous_row_hash + payload). Because every hash
        depends on the one before it, editing or deleting any past change breaks
        every hash after it -- which `verify_integrity` detects.
        """
        new = self.conn.execute(
            f"SELECT * FROM {META_PREFIX}changes WHERE row_hash IS NULL "
            f"ORDER BY change_id"
        ).fetchall()
        if not new:
            return
        prev = self.conn.execute(
            f"SELECT row_hash FROM {META_PREFIX}changes WHERE row_hash IS NOT NULL "
            f"ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["row_hash"] if prev else "GENESIS"
        for r in new:
            h = hashlib.sha256(
                (prev_hash + "|" + self._payload(r)).encode("utf-8")
            ).hexdigest()
            self.conn.execute(
                f"UPDATE {META_PREFIX}changes SET prev_hash=?, row_hash=? "
                f"WHERE change_id=?",
                (prev_hash, h, r["change_id"]),
            )
            prev_hash = h

    def verify_integrity(self) -> dict[str, Any]:
        """Recompute the whole hash chain and report whether it is intact."""
        rows = self.conn.execute(
            f"SELECT * FROM {META_PREFIX}changes ORDER BY change_id"
        ).fetchall()
        prev_hash = "GENESIS"
        for r in rows:
            expect = hashlib.sha256(
                (prev_hash + "|" + self._payload(r)).encode("utf-8")
            ).hexdigest()
            if r["row_hash"] != expect or (r["prev_hash"] or "GENESIS") != prev_hash:
                return {"ok": False, "broken_at": r["change_id"], "total": len(rows)}
            prev_hash = r["row_hash"]
        return {"ok": True, "total": len(rows), "head": prev_hash}

    # ------------------------------------------------------------------ #
    # anomaly detection
    # ------------------------------------------------------------------ #
    def anomalies(self, threshold: int = 5) -> list[dict[str, Any]]:
        """Flag transactions that changed a suspicious number of rows at once
        (e.g. a runaway DELETE/UPDATE, like a bad deployment)."""
        rows = self.conn.execute(
            f"""
            SELECT txn_id, op, COUNT(*) AS rows_affected, MIN(ts) AS ts,
                   MAX(author) AS author, MAX(message) AS message, tbl
            FROM {META_PREFIX}changes
            WHERE op IN ('DELETE','UPDATE')
            GROUP BY txn_id, op
            HAVING rows_affected >= ?
            ORDER BY rows_affected DESC
            """,
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # audit report data
    # ------------------------------------------------------------------ #
    def report_rows(self, **filters) -> list[dict[str, Any]]:
        """Change rows for an audit report (same filters as log, no limit)."""
        filters.setdefault("limit", 1_000_000)
        return list(reversed(self.log(**filters)))  # chronological order

    # ------------------------------------------------------------------ #
    # branching & merging (git-style, file-level fork + 3-way merge)
    # ------------------------------------------------------------------ #
    def _branch_path(self, name: str) -> str:
        base, ext = os.path.splitext(self.path)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        return f"{base}.{safe}{ext or '.sqlite'}"

    def branch(self, name: str, author: str = "", message: str = "") -> str:
        """Fork the whole database into a new branch file that shares this
        one's history up to now. Returns the new file's path."""
        newpath = self._branch_path(name)
        self.conn.commit()
        # flush the WAL into the main db file so the copy is complete
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        shutil.copyfile(self.path, newpath)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {META_PREFIX}branches"
            f"(name, path, created_from_ts, created_ts, author) VALUES (?,?,?,?,?)",
            (name, newpath, _now(), _now(), author),
        )
        self.conn.commit()
        return newpath

    def list_branches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT name, path, created_from_ts, created_ts, author "
            f"FROM {META_PREFIX}branches ORDER BY created_ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def _last_change_ts(self, table: str, pk: int) -> str | None:
        row = self.conn.execute(
            f"SELECT MAX(ts) AS ts FROM {META_PREFIX}changes WHERE tbl=? AND pk=?",
            (table, pk),
        ).fetchone()
        return row["ts"] if row else None

    def merge(self, branch_name: str, author: str = "merge", message: str = "",
              strategy: str = "manual") -> dict[str, Any]:
        """Three-way merge another branch's changes back into this database.

        Uses the fork point as the common base. A row the source branch changed
        is applied cleanly if we haven't touched it since the fork. When *both*
        sides changed the same row, ``strategy`` decides:

            manual  -- report the conflict, keep our version (default)
            ours    -- keep our version (auto-resolved, no conflict reported)
            theirs  -- take the branch's version
            newest  -- take whichever side changed the row most recently
        """
        if strategy not in ("manual", "ours", "theirs", "newest"):
            raise TimeMachineError(f"unknown merge strategy: {strategy}")
        row = self.conn.execute(
            f"SELECT path, created_from_ts FROM {META_PREFIX}branches WHERE name=?",
            (branch_name,),
        ).fetchone()
        if not row:
            raise TimeMachineError(f"unknown branch: {branch_name}")
        fork_ts = row["created_from_ts"]
        src = TimeMachine(row["path"])
        summary = {"applied": 0, "conflicts": [], "resolved": [],
                   "tables": [], "strategy": strategy}

        def resolve(table, rid, ours_val, theirs_val):
            """Decide a conflicting row. Returns (apply?, value)."""
            if strategy == "theirs":
                return True, theirs_val
            if strategy == "ours":
                return False, None
            if strategy == "newest":
                their_ts = src._last_change_ts(table, rid) or ""
                our_ts = self._last_change_ts(table, rid) or ""
                if their_ts > our_ts:
                    return True, theirs_val
                return False, None
            return None, None  # manual -> conflict

        try:
            shared = set(self.user_tables()) & set(src.user_tables())
            for table in sorted(shared):
                base = self._as_of_by_pk(table, fork_ts)      # state at fork
                theirs = src._live_rows_by_pk(table)           # source now
                ours = self._live_rows_by_pk(table)            # target now
                to_apply: dict[int, dict | None] = {}

                def handle_conflict(rid, ours_val, theirs_val):
                    apply, val = resolve(table, rid, ours_val, theirs_val)
                    if apply is None:  # manual
                        summary["conflicts"].append(
                            {"table": table, "pk": rid,
                             "ours": ours_val, "theirs": theirs_val})
                    else:
                        if apply:
                            to_apply[rid] = val
                        summary["resolved"].append(
                            {"table": table, "pk": rid, "took": strategy})

                for rid, s in theirs.items():
                    b, t = base.get(rid), ours.get(rid)
                    if s == b:
                        continue                               # source unchanged
                    if t == b:
                        to_apply[rid] = s                      # clean: source wins
                    elif t != s:
                        handle_conflict(rid, t, s)
                for rid in base:                               # source deletions
                    if rid not in theirs and rid in ours:
                        if ours[rid] == base[rid]:
                            to_apply[rid] = None               # clean delete
                        else:
                            handle_conflict(rid, ours[rid], None)
                if to_apply:
                    self._apply_states(table, to_apply, author,
                                       message or f"merge {branch_name} ({strategy})")
                    summary["applied"] += len(to_apply)
                    summary["tables"].append(table)
        finally:
            src.close()
        return summary

    def _apply_states(self, table: str, states: dict[int, dict | None],
                      author: str, message: str) -> None:
        """Force `table` rows (by rowid) to given states (None = delete),
        recorded through the normal write path."""
        cols = [c["name"] for c in self.columns(table)]
        alias = self._integer_pk_alias(table)
        qt = self._quote_ident(table)

        def body(txn_id: int) -> None:
            live = self._live_rows_by_pk(table)
            for rid, want in states.items():
                if want is None:
                    if rid in live:
                        self.conn.execute(f"DELETE FROM {qt} WHERE rowid=?", (rid,))
                elif rid not in live:
                    if alias:
                        ins, vals = cols, [want.get(c) for c in cols]
                    else:
                        ins, vals = ["rowid"] + cols, [rid] + [want.get(c) for c in cols]
                    collist = ", ".join(self._quote_ident(c) for c in ins)
                    ph = ", ".join(["?"] * len(ins))
                    self.conn.execute(
                        f"INSERT INTO {qt} ({collist}) VALUES ({ph})", vals)
                else:
                    setc = [c for c in cols if c != alias]
                    assign = ", ".join(f"{self._quote_ident(c)}=?" for c in setc)
                    self.conn.execute(
                        f"UPDATE {qt} SET {assign} WHERE rowid=?",
                        [want.get(c) for c in setc] + [rid])

        self._run(author, message, body)

    # ------------------------------------------------------------------ #
    # retention / compaction
    # ------------------------------------------------------------------ #
    def compact(self, before: str, author: str = "system") -> dict[str, Any]:
        """Bound history growth: collapse all changes at or before `before`
        into a single baseline snapshot per row.

        Time travel and reconstruction for any point **at or after** `before`
        stay correct; the fine-grained history *before* the cutoff is discarded
        on purpose (that is the whole point of retention). Because this
        deliberately rewrites the log, the tamper-evident hash chain is rebuilt
        and the compaction is recorded in `_dtm_meta`.
        """
        before = self.resolve_time(before)
        # 1. capture each table's exact state at the cutoff
        states = {t: self._as_of_by_pk(t, before) for t in self.user_tables()}
        # 2. how much are we removing?
        removed = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {META_PREFIX}changes WHERE ts <= ?",
            (before,),
        ).fetchone()["n"]
        # 3. drop the old changes and lay down baseline snapshots at the cutoff
        self.conn.execute(f"DELETE FROM {META_PREFIX}changes WHERE ts <= ?", (before,))
        kept = 0
        for table, rows in states.items():
            for pk, row in rows.items():
                self.conn.execute(
                    f"INSERT INTO {META_PREFIX}changes"
                    f"(txn_id, ts, tbl, pk, op, old_json, new_json, author, message) "
                    f"VALUES (NULL, ?, ?, ?, 'INSERT', NULL, ?, ?, 'compaction baseline')",
                    (before, table, pk, json.dumps(row), author),
                )
                kept += 1
        # 4. rebuild the hash chain over the compacted log
        self.conn.execute(
            f"UPDATE {META_PREFIX}changes SET row_hash=NULL, prev_hash=NULL")
        self._hash_new_changes()
        self.conn.execute(
            f"INSERT OR REPLACE INTO {META_PREFIX}meta(key, value) VALUES "
            f"('last_compaction', ?)",
            (json.dumps({"cutoff": before, "at": _now(),
                         "removed": removed, "kept": kept}),),
        )
        self.conn.commit()
        return {"cutoff": before, "removed": removed, "baseline_rows": kept}

    def close(self) -> None:
        self.conn.close()

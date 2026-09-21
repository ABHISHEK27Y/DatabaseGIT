"""Command-line interface for the Database Time Machine.

Usage examples:

    python -m dtm init mydb.sqlite
    python -m dtm exec mydb.sqlite --author alice -m "create products" \
        "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)"
    python -m dtm exec mydb.sqlite --author alice -m "add widget" \
        "INSERT INTO products(name, price) VALUES('Widget', 9.99)"
    python -m dtm log mydb.sqlite
    python -m dtm as-of mydb.sqlite products --at 2026-09-19T12:00:00+00:00
    python -m dtm history mydb.sqlite products 1
    python -m dtm blame mydb.sqlite products 1 price
    python -m dtm diff mydb.sqlite products --from <ts1> --to <ts2>
    python -m dtm schema-log mydb.sqlite
    python -m dtm schema-blame mydb.sqlite products price
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .core import TimeMachine, TimeMachineError


def _default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = {c: len(c) for c in columns}
    for r in rows:
        for c in columns:
            widths[c] = max(widths[c], len(str(r.get(c, ""))))
    header = "  ".join(c.ljust(widths[c]) for c in columns)
    print(header)
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dtm",
        description="Database Time Machine -- git-style history & time travel for SQLite.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_db(sp):
        sp.add_argument("db", help="path to the SQLite database file")

    sp = sub.add_parser("init", help="initialise time-machine tracking on a database")
    add_db(sp)

    sp = sub.add_parser("exec", help="run attributed SQL (DDL/DML) through the time machine")
    add_db(sp)
    sp.add_argument("sql", help="SQL statement(s) to execute")
    sp.add_argument("--author", "-a", default=None, help="who is making the change")
    sp.add_argument("--message", "-m", default="", help="why (like a commit message)")

    sp = sub.add_parser("query", help="run a read-only query against the live database")
    add_db(sp)
    sp.add_argument("sql", help="SELECT statement")

    sp = sub.add_parser("log", help="show recent changes")
    add_db(sp)
    sp.add_argument("--table", "-t", default=None, help="restrict to one table")
    sp.add_argument("--limit", "-n", type=int, default=20)

    sp = sub.add_parser("as-of", help="reconstruct a table's state at a point in time")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("--at", required=True,
                    help="ISO timestamp, a tag name, or 'now' (see `log`/`tags`)")

    sp = sub.add_parser("history", help="full change timeline of a single row")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("pk", type=int, help="rowid of the row")

    sp = sub.add_parser("blame", help="who last changed a column of a row, and why")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("pk", type=int)
    sp.add_argument("column")

    sp = sub.add_parser("diff", help="diff a table between two timestamps")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("--from", dest="frm", required=True, help="timestamp / tag / 'now'")
    sp.add_argument("--to", required=True, help="timestamp / tag / 'now'")

    sp = sub.add_parser("revert", help="restore a table to a past state (recorded)")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("--to", required=True, help="timestamp / tag / 'now'")
    sp.add_argument("--author", "-a", default=None)
    sp.add_argument("--message", "-m", default="")

    sp = sub.add_parser("tag", help="create a named checkpoint at the current moment")
    add_db(sp)
    sp.add_argument("name")
    sp.add_argument("--author", "-a", default=None)
    sp.add_argument("--message", "-m", default="")
    sp.add_argument("--at", default=None, help="tag a past timestamp instead of now")

    sp = sub.add_parser("tags", help="list named checkpoints")
    add_db(sp)

    sp = sub.add_parser("stats", help="summary statistics about the change history")
    add_db(sp)

    sp = sub.add_parser("serve", help="launch the web UI")
    add_db(sp)
    sp.add_argument("--port", "-p", type=int, default=8080)
    sp.add_argument("--host", default="127.0.0.1")

    sp = sub.add_parser("tables", help="list tracked tables")
    add_db(sp)

    sp = sub.add_parser("schema-log", help="show schema-change history")
    add_db(sp)

    sp = sub.add_parser("schema-blame", help="when a column was added/changed, and by whom")
    add_db(sp)
    sp.add_argument("table")
    sp.add_argument("column")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tm = TimeMachine(args.db)
    try:
        return _dispatch(tm, args)
    except TimeMachineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        tm.close()


def _dispatch(tm: TimeMachine, args) -> int:
    cmd = args.cmd

    if cmd == "init":
        tm.init_repo()
        print(f"Initialised time-machine on {args.db}")
        tables = tm.user_tables()
        print(f"Tracking {len(tables)} table(s): {', '.join(tables) or '(none yet)'}")
        return 0

    if cmd == "exec":
        author = args.author or _default_author()
        tm.exec_sql(args.sql, author=author, message=args.message)
        print(f"OK  (author={author}, message={args.message!r})")
        return 0

    if cmd == "query":
        rows = tm.query(args.sql)
        data = [dict(r) for r in rows]
        cols = list(data[0].keys()) if data else []
        _print_table(data, cols)
        return 0

    if cmd == "log":
        rows = tm.log(table=args.table, limit=args.limit)
        _print_table(
            rows,
            ["change_id", "ts", "tbl", "pk", "op", "author", "message"],
        )
        return 0

    if cmd == "as-of":
        rows = tm.as_of(args.table, tm.resolve_time(args.at))
        cols = list(rows[0].keys()) if rows else []
        _print_table(rows, cols)
        return 0

    if cmd == "history":
        rows = tm.row_history(args.table, args.pk)
        _print_json(rows)
        return 0

    if cmd == "blame":
        res = tm.blame(args.table, args.pk, args.column)
        if res is None:
            print("No change found for that row/column.")
        else:
            print(
                f"{args.table}.{args.column} of row {args.pk} was last set by "
                f"'{res['author']}' at {res['ts']}\n"
                f"  {res['old_value']!r} -> {res['new_value']!r}\n"
                f"  message: {res['message']!r}  (op={res['op']}, "
                f"change #{res['change_id']})"
            )
        return 0

    if cmd == "diff":
        _print_json(
            tm.diff(args.table, tm.resolve_time(args.frm), tm.resolve_time(args.to))
        )
        return 0

    if cmd == "revert":
        author = args.author or _default_author()
        summary = tm.revert(args.table, args.to, author=author, message=args.message)
        print(
            f"Reverted {args.table} to {args.to}  "
            f"(inserted={summary['inserted']}, updated={summary['updated']}, "
            f"deleted={summary['deleted']}, author={author})"
        )
        return 0

    if cmd == "tag":
        author = args.author or _default_author()
        ts = tm.tag(args.name, author=author, message=args.message, at=args.at)
        print(f"Tag '{args.name}' -> {ts}  (author={author})")
        return 0

    if cmd == "tags":
        _print_table(tm.list_tags(), ["name", "ts", "author", "message"])
        return 0

    if cmd == "stats":
        _print_json(tm.stats())
        return 0

    if cmd == "serve":
        from .web import serve
        serve(tm, host=args.host, port=args.port)
        return 0

    if cmd == "tables":
        for t in tm.user_tables():
            print(t)
        return 0

    if cmd == "schema-log":
        _print_table(tm.schema_log(), ["schema_id", "ts", "author", "message"])
        return 0

    if cmd == "schema-blame":
        res = tm.schema_blame(args.table, args.column)
        if res is None:
            print("No schema record for that column.")
        else:
            _print_json(res)
        return 0

    raise TimeMachineError(f"unknown command: {cmd}")


if __name__ == "__main__":
    sys.exit(main())

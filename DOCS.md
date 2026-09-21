# Database Time Machine — Technical Documentation

> "Git for databases." A tool that records every schema and data change made to a
> SQLite database — with **who**, **when**, **why**, and **what changed** — so you
> can travel back to any point in the database's history and answer questions an
> ordinary database throws away.

This document explains **how the system works internally**, component by component,
followed by a note on **prior art in the market** and **what is original here**.

---

## Table of contents

1. [The problem](#1-the-problem)
2. [Core idea in one picture](#2-core-idea-in-one-picture)
3. [Data model — the metadata tables](#3-data-model--the-metadata-tables)
4. [How a write is captured (the full path)](#4-how-a-write-is-captured-the-full-path)
5. [How time travel is reconstructed](#5-how-time-travel-is-reconstructed)
6. [Attribution — "who" without database users](#6-attribution--who-without-database-users)
7. [Schema-change tracking](#7-schema-change-tracking)
8. [Command reference](#8-command-reference)
9. [Code map](#9-code-map)
10. [Worked example (the demo, step by step)](#10-worked-example-the-demo-step-by-step)
11. [What works, and what does NOT](#11-what-works-and-what-does-not)
12. [Prior art and originality](#12-prior-art-and-originality)
13. [Future work](#13-future-work)

---

## 1. The problem

A normal database stores only the **current** state. When you run
`UPDATE users SET plan='enterprise'` or `DELETE FROM users WHERE plan='free'`, the
previous values are gone forever. That makes three everyday questions impossible
to answer after the fact:

- **"Who changed this column, and why?"**
- **"Why did this data disappear after Tuesday's deployment?"**
- **"What did this table look like last Monday?"**

The Database Time Machine keeps a complete, append-only record of every change so
all three become one-command queries.

---

## 2. Core idea in one picture

```
        python -m dtm exec  --author alice  -m "raise price"  "UPDATE ..."
                                   │
             (1) write context     │
                                   ▼
        ┌────────────────────────────────────────────┐
        │ _dtm_context (single row):                   │
        │   author = alice, message = "raise price",   │
        │   txn_id = 42, ts = 2026-09-19T15:07:...Z    │
        └────────────────────────────────────────────┘
                                   │  read by triggers
             (2) run the SQL       ▼
   ┌────────────┐   AFTER INSERT / UPDATE / DELETE   ┌──────────────────────┐
   │ user table │ ────────── triggers fire ───────▶ │ _dtm_changes         │
   │ (products) │                                    │ (append-only log)    │
   └────────────┘                                    │  old_json, new_json, │
                                                      │  author, message, ts │
                                                      └──────────────────────┘
```

Every change ends up as an immutable row in `_dtm_changes` containing the **entire
row before** and the **entire row after** the change, as JSON, tagged with author,
message and timestamp. Nothing is ever overwritten — the log *is* the history.

---

## 3. Data model — the metadata tables

All engine tables are prefixed `_dtm_` so they never collide with user tables.
They are created by `init_repo()` in [`dtm/core.py`](dtm/core.py).

| Table | Role |
|---|---|
| `_dtm_meta` | Key/value store (e.g. schema `version`). |
| `_dtm_context` | **Single-row** scratch table. The CLI writes `author`, `message`, `txn_id`, `ts` here *before* running any SQL, so triggers can read them. |
| `_dtm_txn` | One row per `exec` call — the equivalent of a git "commit". |
| `_dtm_changes` | **The heart of the tool.** Append-only row-level change log. |
| `_dtm_tracked` | Which user tables are tracked, and since when. |
| `_dtm_schema` | Structured schema snapshots, one per detected schema change. |

### `_dtm_changes` columns

| Column | Meaning |
|---|---|
| `change_id` | Monotonic primary key. Because it always increases, "latest change" = "max `change_id`". |
| `txn_id` | Which commit this change belonged to. |
| `ts` | ISO-8601 UTC timestamp (microsecond precision). |
| `tbl` | The affected user table. |
| `pk` | The affected row's `rowid`. |
| `op` | `INSERT`, `UPDATE`, or `DELETE`. |
| `old_json` | The full row **before** the change (`NULL` for INSERT). |
| `new_json` | The full row **after** the change (`NULL` for DELETE). |
| `author` / `message` | Attribution copied from `_dtm_context`. |

> **Why ISO-8601 UTC strings?** In this exact format, *lexicographic* (alphabetical)
> string comparison equals *chronological* comparison. That lets time-travel
> queries use a plain `ts <= ?` filter with no date parsing.

---

## 4. How a write is captured (the full path)

Writes go through `TimeMachine.exec_sql(sql, author, message)`. The sequence:

1. **Stamp a timestamp** `ts = _now()` (UTC, ISO-8601).
2. **Open a transaction row** in `_dtm_txn` → obtain `txn_id` (the "commit id").
3. **Write the context** into the single-row `_dtm_context` table:
   `(author, message, txn_id, ts)`.
4. **Execute the user SQL** with `executescript()` (so multiple statements and DDL
   are allowed in one call).
5. As each row is inserted/updated/deleted, the **triggers fire** and append a row
   to `_dtm_changes`, reading `author`/`message`/`txn_id`/`ts` from `_dtm_context`.
6. **`resync_tracking()`** installs triggers on any *new* table created by this SQL
   and refreshes triggers if a table's columns changed.
7. **`_maybe_snapshot_schema()`** compares the current schema to the last snapshot
   and records a new one if it changed.
8. **Commit.** On any SQL error the whole thing is rolled back.

### The triggers

For every tracked table `T`, three triggers are installed (see
`_install_triggers()`), for example the UPDATE trigger:

```sql
CREATE TRIGGER _dtm_T_upd AFTER UPDATE ON "T"
BEGIN
    INSERT INTO _dtm_changes
        (txn_id, ts, tbl, pk, op, old_json, new_json, author, message)
    VALUES (
        (SELECT txn_id  FROM _dtm_context WHERE id=1),
        (SELECT ts      FROM _dtm_context WHERE id=1),
        'T', NEW.rowid, 'UPDATE',
        json_object('col1', OLD.col1, 'col2', OLD.col2, ...),   -- old row
        json_object('col1', NEW.col1, 'col2', NEW.col2, ...),   -- new row
        (SELECT author  FROM _dtm_context WHERE id=1),
        (SELECT message FROM _dtm_context WHERE id=1)
    );
END;
```

The `json_object(...)` expression is generated dynamically for each table by
reading its columns from `PRAGMA table_info` (`_json_object_expr()`), so it always
matches the table's real shape. It relies on SQLite's built-in **JSON1** extension
(present in modern Python builds; `_check_json1()` verifies it at startup).

### Baseline snapshot

When a table is first tracked (`resync_tracking()` → `_baseline_table()`), its rows
that *already exist* are written into `_dtm_changes` as synthetic `INSERT` records
labelled `"baseline snapshot"`. Without this, time-travel would have no "creation
event" for pre-existing data and could not reconstruct it.

---

## 5. How time travel is reconstructed

`as_of(table, at)` rebuilds a table exactly as it was at timestamp `at`:

> For each row (keyed by `rowid`), take the **most recent change at or before `at`**.
> If that change was a `DELETE`, the row does not exist at that time. Otherwise the
> change's `new_json` is the row's state.

The SQL uses a correlated subquery to pick the latest change per row:

```sql
SELECT c.pk, c.op, c.new_json
FROM _dtm_changes c
WHERE c.tbl = ? AND c.ts <= ?
  AND c.change_id = (
      SELECT MAX(c2.change_id) FROM _dtm_changes c2
      WHERE c2.tbl = c.tbl AND c2.pk = c.pk AND c2.ts <= ?
  );
```

Because `change_id` increases in insertion order and `ts` increases with it, "max
`change_id` among rows with `ts <= at`" is exactly "the last change up to that time".

Built on the same primitive:

- **`row_history(table, pk)`** — every change to one row, in order (the full story).
- **`diff(table, from, to)`** — reconstruct the table at both timestamps and report
  `added` / `removed` / `changed` rows.

---

## 6. Attribution — "who" without database users

SQLite has **no concept of a logged-in user**, so a trigger cannot ask "who is
doing this?". The Time Machine solves this cooperatively:

- The CLI (or any caller) writes the author/message into `_dtm_context` *before*
  running SQL (`exec --author alice -m "..."`).
- Triggers read that single row, so every logged change carries an author.

**Trade-off (documented honestly):** attribution is only as truthful as the caller.
A write made by another program that bypasses `dtm exec` is still **captured** by
the triggers, but it is attributed to whatever context was last set. In a real
multi-user database (Postgres/MySQL) this would instead come from the session's
authenticated user.

---

## 7. Schema-change tracking

After every `exec`, `_maybe_snapshot_schema()` builds a structured snapshot of the
current schema — for each user table, its columns from `PRAGMA table_info`
(`name`, `type`, `notnull`, `dflt`, `pk`) — and stores it as JSON in `_dtm_schema`
**only if it differs** from the previous snapshot.

This powers:

- **`schema-log`** — the timeline of schema changes, with author and message.
- **`schema-blame(table, column)`** — walks the snapshots to find when a column
  first appeared or changed definition, and who did it. This is the schema-level
  answer to "who changed this column?".

---

## 8. Command reference

Invoke as `python -m dtm <command> <db> [...]`.

| Command | Answers / does |
|---|---|
| `init` | Sets up tracking on a database (creates metadata tables + triggers). |
| `exec` | Runs attributed SQL (DDL/DML). **The only intended way to write.** |
| `query` | Read-only `SELECT` against the live database. |
| `log` | Recent changes (all tables, or `--table`). |
| `history` | Every change to one row (`table pk`), in order. |
| `blame` | Who last changed a given column of a row, with old→new and why. |
| `as-of` | Reconstruct a table's full state at `--at <timestamp/tag/now>`. |
| `diff` | `--from`/`--to` — added / removed / changed rows between two times. |
| `revert` | Restore a table to a past state (`--to <ts/tag/now>`); the revert is itself recorded. |
| `tag` | Create a named checkpoint (`dtm tag <db> <name>`) you can time-travel to. |
| `tags` | List named checkpoints. |
| `stats` | Summary statistics (changes per author/table/op/day, most-edited rows). |
| `serve` | Launch the **web UI** (`--port`, `--host`). |
| `tables` | List tracked tables. |
| `schema-log` | Schema-change history. |
| `schema-blame` | When a column was added/changed, and by whom. |

Timestamps are the ISO strings shown in `log` output. Anywhere a timestamp is
accepted you may also pass a **tag name** or the literal **`now`**.

### Revert, Tags, Stats & Web UI (detailed)

**Revert — turn "view the past" into "undo".**
```bash
python -m dtm revert mydb.sqlite users --to before-deploy -a carol -m "restore"
```
Reconstructs the target state, then applies the minimal INSERT/UPDATE/DELETE to
make the live table match — *through the normal write path*, so the revert is
recorded as ordinary changes (you can revert a revert). Deleted rows are restored
with their **original `rowid` identity**.

**Tags — name a moment in time.**
```bash
python -m dtm tag mydb.sqlite before-deploy -m "known-good point"   # tag "now"
python -m dtm tag mydb.sqlite v1 --at 2026-09-19T12:00:00+00:00     # tag a past time
python -m dtm tags mydb.sqlite
```
Then use the name anywhere a timestamp is expected: `as-of ... --at before-deploy`,
`diff --from v1 --to now`, `revert ... --to before-deploy`.

**Stats — dashboard data.** `python -m dtm stats mydb.sqlite` returns totals,
per-author / per-table / per-operation / per-day counts, and the most-edited rows.

**Web UI — visual, zero-dependency.**
```bash
python -m dtm serve mydb.sqlite --port 8080
# open http://127.0.0.1:8080
```
Built on the standard-library `http.server` (no Flask, no `pip install`). It serves
a JSON API and a single-page app with Overview (stats + charts), Timeline, Time
Travel, Diff, Blame, Schema and Tags views. The UI is read-only for safety — the
only write path (revert) stays on the CLI. See
[ARCHITECTURE.md](ARCHITECTURE.md) §9 for how it stays thread-safe.

---

## 9. Code map

```
dtm/
  core.py      TimeMachine class — all engine logic:
               init_repo, triggers, exec_sql/_run, as_of, blame, diff,
               row_history, schema snapshots, revert, tags, stats. Pure stdlib.
  cli.py       argparse command-line interface + table/JSON printers.
  web.py       Zero-dependency web UI: stdlib http.server JSON API + SPA.
  __main__.py  Enables `python -m dtm`.
  __init__.py  Package exports + version.

demo.py          Runnable end-to-end story (Section 10).
tests/
  test_dtm.py    12 unit tests: logging, blame, time-travel as-of, deletes,
                 baseline, diff, schema-blame, revert, tags, resolution, stats.
README.md        Quick start + overview.
DOCS.md          This document (feature reference).
ARCHITECTURE.md  Layered design, the how/why of every major decision.
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

---

## 10. Worked example (the demo, step by step)

`python demo.py` builds `demo.sqlite` and runs this narrative:

1. **Monday** — `alice` creates a `users` table and adds Ravi (`free`) and Meera (`pro`).
   A timestamp `monday_snapshot` is captured.
2. **Tuesday** — `bob`'s deployment runs `DELETE FROM users WHERE plan='free'`
   (a too-broad "cleanup") and upgrades Meera to `enterprise`.
3. **Time travel** — `as-of monday_snapshot` shows *both* users as they were, even
   though Ravi is gone from the live table.
4. **Row history** — `history users 1` shows Ravi was INSERTed by alice, then
   DELETEd by bob with the message `"deploy: cleanup script (BUG: too broad)"` —
   directly answering "why did the data disappear?".
5. **Blame** — `blame users 2 plan` reports Meera's plan went `'pro' → 'enterprise'`,
   by bob, with his message.
6. **Diff** — across the deploy: Ravi `removed`, Meera `changed`.
7. **Schema history** — `carol` later adds a `signup_date` column; `schema-log` and
   `schema-blame` attribute it to her.

All of this is real output verified by the test suite.

---

## 11. What works, and what does NOT

### Works (implemented and tested)
- Row-level capture of every INSERT/UPDATE/DELETE with full before/after values.
- Author + message + timestamp attribution per change.
- Point-in-time reconstruction of any table (`as-of`).
- Per-row history, per-column blame, and table diffs between two times.
- **One-command `revert`** to any past state, restoring original row identity, with
  the revert itself recorded.
- **Named checkpoints (`tag`)** usable anywhere a timestamp is expected.
- **Stats** (per author/table/op/day, most-edited rows).
- **Zero-dependency web UI** (`serve`) with dashboard, time travel, diff and blame.
- Automatic tracking of newly created tables and refreshed triggers on column change.
- Structured schema snapshots with schema-level log and blame.
- Zero external dependencies; runs on plain Python 3.9+ / SQLite.

### Does NOT (deliberate limitations, stated honestly)
- **No branching or merging** of database state — the hard part of the "git for
  databases" dream remains a documented stretch goal (see ARCHITECTURE §11).
- **Attribution is cooperative** (see Section 6), not enforced by real DB auth.
- **Storage grows with history** — every version of every changed row is kept; no
  compaction/retention policy yet.
- **Web UI is read-only** — the one write path (revert) is intentionally CLI-only.
- **Assumes ordinary `rowid` tables** (not `WITHOUT ROWID` tables).
- **SQLite only** — no Postgres/MySQL adapters yet.

---

## 12. Prior art and originality

The phrase "Git for databases" describes a real, established category. Being aware
of it is a strength, not a weakness — here is how this project relates to it.

**Existing tools in the market:**

- **Dolt** — a full SQL database with real git-style branch/merge/clone/diff on both
  schema and data. The most direct commercial embodiment of the idea.
- **Temporal / system-versioned tables** — SQL:2011 feature in SQL Server, MariaDB,
  and DB2; the database keeps historical row versions for time-travel queries.
- **Change Data Capture (CDC)** — Debezium, and Postgres logical decoding / MySQL
  binlog readers stream every change out of a database for auditing/replication.
- **Audit triggers** — the classic pattern of shadow "history" tables populated by
  triggers, used across many production systems.
- **Liquibase / Flyway** — version *schema migrations*, but not row-level data.

**What this project is, honestly:** an **educational, from-scratch implementation**
that shows the *mechanism* behind these tools, using nothing but SQLite triggers and
the standard library. It is not a competitor to Dolt.

**What is genuinely its own contribution:**

1. **A single, readable engine** (~one file) that combines four ideas usually found
   separately — trigger-based audit logging, cooperative author attribution,
   point-in-time reconstruction, and structured schema-snapshot blame.
2. **Zero-dependency and zero-setup** — no server, no extensions, no `pip install`;
   it runs anywhere Python does, which makes it a clear teaching/demo artefact.
3. **A CLI framed around the *questions*** ("blame", "as-of", "history", "diff")
   rather than around raw log tables — the value is in how the history is queried.

In short: the *category* is well-known; the value here is a compact, transparent,
self-contained implementation you can read end-to-end and extend — appropriate for
a course project and a portfolio piece, with prior art acknowledged rather than
ignored.

---

## 13. Future work

- **`dtm revert <table> --to <ts>`** — restore a table to a past state (write the
  reconstructed rows back through `exec` so the revert is itself recorded).
- **Branching & merging** of database state — the full "git for databases" goal.
- **A web UI** to browse the timeline, diffs, and blame visually (great for demos).
- **Adapters for PostgreSQL** (logical decoding / WAL) and **MySQL** (binlog), where
  attribution can come from the real authenticated session user.
- **Retention / compaction** policies to bound history growth.

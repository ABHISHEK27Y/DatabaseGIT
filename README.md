# Database Time Machine

**Git for databases.** Track every schema and data change to a SQLite database,
with author + message + timestamp, and answer questions no ordinary database can:

- **"Who changed this column, and why?"**
- **"Why did user data disappear after Tuesday's deployment?"**
- **"Show me the state of this table on August 20."**

Pure Python standard library — no external dependencies, no database server.
Runs anywhere Python 3.9+ runs.

---

## Quick start

```bash
# 1. Initialise tracking on a database (creates it if missing)
python -m dtm init mydb.sqlite

# 2. Make changes THROUGH the time machine so they get attributed
python -m dtm exec mydb.sqlite -a alice -m "create products" \
    "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)"

python -m dtm exec mydb.sqlite -a alice -m "add widget" \
    "INSERT INTO products(name, price) VALUES('Widget', 9.99)"

python -m dtm exec mydb.sqlite -a bob -m "raise price" \
    "UPDATE products SET price = 12.5 WHERE id = 1"

# 3. Ask the interesting questions
python -m dtm log     mydb.sqlite                    # recent changes
python -m dtm blame   mydb.sqlite products 1 price   # who last changed a column
python -m dtm history mydb.sqlite products 1         # full timeline of a row
python -m dtm as-of   mydb.sqlite products --at <ts> # table as it was at a time
python -m dtm diff    mydb.sqlite products --from <ts1> --to <ts2>

# 4. Undo, checkpoint, and explore
python -m dtm tag     mydb.sqlite before-deploy -m "known-good"   # name a moment
python -m dtm revert  mydb.sqlite products --to before-deploy -a you  # one-command undo
python -m dtm stats   mydb.sqlite                    # activity summary
python -m dtm serve   mydb.sqlite --port 8080        # visual web UI (no deps)

# 5. Trust, monitor, branch, and report
python -m dtm verify    mydb.sqlite                  # is the audit log un-tampered?
python -m dtm anomalies mydb.sqlite -n 10            # flag suspicious mass changes
python -m dtm log       mydb.sqlite --author bob --op DELETE --contains price  # search
python -m dtm branch    mydb.sqlite experiment       # fork the database
python -m dtm merge     mydb.sqlite experiment -s newest   # 3-way merge, auto-resolve
python -m dtm compact   mydb.sqlite --before 2026-01-01T00:00:00+00:00  # retention
python -m dtm report    mydb.sqlite -f html -o audit.html     # audit report
```

> Anywhere a timestamp is accepted you can pass a **tag name** or **`now`** instead.

## Web UI

`python -m dtm serve mydb.sqlite` starts a zero-dependency web dashboard
(stdlib `http.server`, no Flask) at <http://127.0.0.1:8080> with Overview stats,
Timeline, Time Travel, Diff, Blame, Schema and Tags views.

See the whole story end-to-end:

```bash
python demo.py
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

---

## How it works

```
             python -m dtm exec  (author, message)
                        │
                        ▼
        ┌───────────────────────────────────┐
        │  _dtm_context  (author, msg, txn)  │◀── set before every statement
        └───────────────────────────────────┘
                        │ read by
                        ▼
   user tables ──AFTER INSERT/UPDATE/DELETE triggers──▶  _dtm_changes
                                                          (append-only log:
                                                           old_json, new_json,
                                                           author, msg, ts)
```

1. **`init`** installs metadata tables and, for every user table, three triggers
   (`AFTER INSERT / UPDATE / DELETE`). Existing rows are captured as a *baseline*
   snapshot so history is complete.
2. Before running your SQL, the CLI writes the current **author / message /
   transaction id / timestamp** into a one-row `_dtm_context` table. The triggers
   read from it, so every logged change is attributed — even though SQLite itself
   has no concept of "who".
3. Each change is appended to **`_dtm_changes`** as the full old row and new row
   in JSON. Nothing is ever overwritten — the log is the source of truth.
4. **Time travel** (`as-of`) reconstructs a table at any timestamp: for each row
   it takes the most recent change at or before that time; a `DELETE` means the
   row is gone, otherwise the post-change JSON is its state.
5. **Schema changes** (new tables, `ALTER TABLE ...`) are detected after each
   `exec` and stored as structured snapshots in `_dtm_schema`, powering
   `schema-log` and `schema-blame`.

Rows are keyed internally by SQLite's `rowid`.

---

## Commands

| Command        | What it answers                                             |
|----------------|-------------------------------------------------------------|
| `init`         | Start tracking a database                                   |
| `exec`         | Run attributed SQL (DDL/DML) — the *only* way to write      |
| `query`        | Read-only `SELECT` against the live database                |
| `log`          | Recent changes (optionally one table)                       |
| `history`      | Every change to one row, in order                           |
| `blame`        | Who last changed a given column of a row, and why           |
| `as-of`        | Reconstruct a table's state at a timestamp                  |
| `diff`         | Added / removed / changed rows between two timestamps       |
| `tables`       | List tracked tables                                         |
| `schema-log`   | Schema-change history                                       |
| `schema-blame` | When a column was added/changed, and by whom                |

---

## Project layout

```
dtm/
  core.py     # TimeMachine engine: triggers, time travel, revert, tags, stats,
              #   hash-chain integrity, anomalies, branching/merge, session API
  cli.py      # argparse command-line interface
  web.py      # zero-dependency web UI (stdlib http.server)
  report.py   # HTML / CSV audit-report export
  postgres.py # optional PostgreSQL backend (needs psycopg + a server)
  __main__.py # enables `python -m dtm`
demo.py       # end-to-end walkthrough of every feature
tests/
  test_dtm.py # 19 unit tests for the engine
pyproject.toml            # `pip install .` -> a global `dtm` command
.github/workflows/ci.yml  # tests on Linux + Windows, Python 3.9 / 3.11 / 3.13
```

Install as a real command:

```bash
pip install .    # then use `dtm ...` instead of `python -m dtm ...`
```

See [DOCS.md](DOCS.md) for the full feature reference and
[ARCHITECTURE.md](ARCHITECTURE.md) for the design reasoning (how/why/what).

### Highlights

- **Tamper-evident log** — every change is hash-chained to the previous one, so
  any edit to history is detectable (`dtm verify`).
- **Time travel & revert** — view or restore any table at any past moment.
- **Branching & merge** — fork the database, change it, 3-way merge it back with
  conflict detection.
- **Anomaly detection** — auto-flag runaway mass deletes/updates.
- **Audit reports** — export who-changed-what to HTML or CSV.
- **Zero dependencies** — engine, CLI and web UI are pure standard library
  (PostgreSQL backend is the one optional extra).

---

## Design notes & honest limitations

- **Attribution is cooperative.** SQLite has no users, so authorship is whatever
  the caller passes to `exec`. Writes made by other tools that bypass `dtm exec`
  are still *captured* by the triggers, but attributed to the last context set.
- **Storage grows with history** (every version of every changed row is kept).
  A production version would add compaction/retention policies.
- **Assumes normal `rowid` tables** (not `WITHOUT ROWID`).

## Possible extensions (remaining stretch goals)

- **MySQL** adapter (binlog-based), to sit alongside the PostgreSQL backend.
- **Distributed / streaming** capture for very high write volumes.
- A hosted, multi-database dashboard.

*(Revert, tags, stats, web UI, tamper-evident hash chain, anomaly detection,
branching + 3-way merge with conflict strategies, retention/compaction, audit
reports, search, a programmatic API, the PostgreSQL backend, pip packaging and CI
are all implemented — see above.)*

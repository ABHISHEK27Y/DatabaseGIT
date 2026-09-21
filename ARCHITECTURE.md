# Architecture — Database Time Machine

This document explains **what** the system is built from, **how** the pieces fit
together, and **why** each major decision was made. Read [DOCS.md](DOCS.md) for the
feature-level walkthrough; read this for the design reasoning.

---

## 1. What we are building (in one sentence)

A thin layer over a SQLite database that turns every write into an **immutable,
attributed, queryable event**, so the database gains a complete history and the
ability to travel back in time — the core of what commercial tools call
"git for databases".

---

## 2. Layered architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  PRESENTATION LAYER                                                    │
│                                                                        │
│   CLI  (dtm/cli.py)                Web UI  (dtm/web.py)                │
│   argparse commands                stdlib http.server + single-page    │
│   init/exec/log/blame/as-of/       app (JSON API + vanilla-JS SPA)     │
│   diff/revert/tag/stats/serve                                          │
└───────────────┬─────────────────────────────┬────────────────────────┘
                │                               │
                ▼                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ENGINE LAYER   (dtm/core.py -- class TimeMachine)                     │
│                                                                        │
│   • write path : exec_sql / revert  ── _run() ── attributed txn        │
│   • capture    : per-table AFTER INSERT/UPDATE/DELETE triggers         │
│   • query      : as_of / diff / row_history / blame / log / stats      │
│   • schema     : snapshot + schema_log / schema_blame                  │
│   • tags       : tag / list_tags / resolve_time                        │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │  sqlite3 (stdlib)
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  STORAGE LAYER   (one SQLite file)                                     │
│                                                                        │
│   user tables      ← the real data (unchanged, still normal SQL)      │
│   _dtm_changes     ← append-only event log (old/new JSON + who/why)   │
│   _dtm_txn         ← one row per "commit"                              │
│   _dtm_context     ← single-row scratch: current author/message/txn   │
│   _dtm_schema      ← structured schema snapshots                      │
│   _dtm_tags        ← named checkpoints                                │
│   _dtm_tracked     ← which tables are tracked                         │
└─────────────────────────────────────────────────────────────────────┘
```

**Why layered?** The engine has zero knowledge of the CLI or web UI — both are
just callers of the same `TimeMachine` methods. That means the web UI added no new
logic; it only exposes existing methods over HTTP. A future PostgreSQL backend
would replace only the engine+storage layers.

---

## 3. The central design decision: capture at the database, not the application

There are three common places to record history. We chose the middle one.

| Approach | How | Why we did / didn't use it |
|---|---|---|
| **Application-level** | App code writes audit rows | ✗ Every app must cooperate; misses direct SQL. |
| **Trigger-level (chosen)** | DB triggers log every change | ✓ Captures *all* writes to tracked tables, even outside the tool. Pure SQL, portable. |
| **Log-level (WAL/binlog)** | Read the DB's physical log | ✗ Powerful but engine-specific and complex; overkill for SQLite. |

**Consequence:** because triggers fire inside the database, *any* write to a tracked
table is captured — not only writes made through `dtm exec`. The tool cannot be
"bypassed" for data capture; it can only be bypassed for *attribution* (see §5).

---

## 4. The write path (how a change becomes an event)

Every mutation — whether user SQL or an internal revert — flows through one
method, `_run(author, message, body)`:

```
_run:
  1. ts     = now()                       # single timestamp for the whole txn
  2. txn_id = INSERT INTO _dtm_txn(...)    # the "commit" id
  3. INSERT OR REPLACE _dtm_context        # (author, message, txn_id, ts)
  4. body(txn_id)                          # run the actual SQL / statements
        └─ triggers fire → append rows to _dtm_changes,
           reading author/message/txn_id/ts from _dtm_context
  5. resync_tracking()                     # trigger any newly created tables
  6. maybe_snapshot_schema()               # record schema if it changed
  7. COMMIT   (or ROLLBACK on any error)
```

**Why funnel everything through `_run`?** It guarantees that *no* write can be
recorded without an author, a message, a transaction id, and a timestamp — the
four things that make the log answerable. `exec_sql` and `revert` are both just
different `body` functions.

**Why a single-row `_dtm_context` table?** SQLite triggers cannot read process
memory, environment variables, or function arguments — but they *can* read another
table. The context table is the bridge that carries "who/why" from Python into the
trigger. It is overwritten on every `_run`, so it holds only the *current* txn.

---

## 5. Attribution model (the honest trade-off)

SQLite has no authenticated users, so "who" must be supplied by the caller and
stored where triggers can see it (the context table).

- **Captured but attributed to last context:** a write made by another program that
  bypasses `dtm exec` is still logged by the triggers, but tagged with whatever
  author was last set.
- **Why accept this?** For a single-file, serverless database it is the only
  portable option, and it is honest about its boundary. In a real multi-user engine
  (PostgreSQL/MySQL) the same design slots in `current_user` / session identity
  instead — the architecture does not change, only the source of "who".

---

## 6. Time-travel model (how the past is reconstructed)

The log stores, for every change, the **full row after** the change (`new_json`)
and the **full row before** (`old_json`), keyed by the row's `rowid`. State at time
`T` is derived, never stored separately:

> For each rowid, take the change with the largest `change_id` whose `ts <= T`.
> `DELETE` → row absent; otherwise `new_json` is the row.

**Why store whole rows, not deltas?** Reconstruction becomes a single indexed query
with no need to replay a chain of diffs from the beginning. The cost is storage
(every version is kept) — an explicit, documented trade-off, with compaction listed
as future work.

**Why key by `rowid`?** Every ordinary SQLite table has a stable `rowid`, so the
engine needs no per-table primary-key configuration. Revert uses it to restore a
deleted row with its **original identity** intact.

**Why ISO-8601 UTC strings for time?** In that exact format, lexicographic string
comparison equals chronological comparison, so `ts <= ?` needs no date parsing and
works directly in SQL and in tag resolution.

---

## 7. Revert (how "view the past" becomes "undo")

`revert(table, at)` reconstructs the target state, compares it to the live table,
and applies the minimal set of INSERT/UPDATE/DELETE to reconcile them — **through
`_run`**, so the revert is itself recorded as ordinary changes.

```
target  = as_of_by_pk(table, at)      live = current rows by rowid
for rid in live  not in target        → DELETE
for rid in target not in live          → INSERT (restoring original rowid)
for rid in both, if values differ      → UPDATE
```

**Why record the revert instead of rewriting history?** This is the "git" instinct:
history is append-only. You can revert a revert, and the log always tells the true
story of what happened, including the recovery.

---

## 8. Tags (why name a point in time)

Raw timestamps are precise but unusable by a human ("what was the exact microsecond
before Tuesday's deploy?"). `_dtm_tags` maps a **name → timestamp**, and
`resolve_time(spec)` accepts a tag name, `now`/`HEAD`, or a literal timestamp. Every
time-travel command (`as-of`, `diff`, `revert`) runs its input through it, so tags
work everywhere for free.

---

## 9. Web UI (why stdlib, not Flask)

The project's headline property is **zero dependencies / zero setup**. Introducing
Flask would break that for the one component users are most likely to run. So the
UI is built on `http.server.ThreadingHTTPServer`:

- A tiny router maps `/api/*` paths to `TimeMachine` methods and returns JSON.
- A single embedded HTML page (vanilla JS + CSS, no external assets) renders the
  Overview, Timeline, Time Travel, Diff, Blame, Schema and Tags views.
- **Thread-safety:** SQLite connections are not shareable across threads, so each
  HTTP request opens its own short-lived `TimeMachine` connection and closes it.
  The UI is read-only; the only write operation (revert) is deliberately kept on
  the CLI so the browser cannot mutate data by accident.

---

## 10. Module structure

```
dtm/
  core.py    Engine. The only file that talks to SQLite. All history logic.
  cli.py     Thin argparse front-end; formats engine output for the terminal.
  web.py     Thin HTTP front-end; JSON API + embedded single-page app.
  __main__.py / __init__.py   packaging (`python -m dtm`, exports).
tests/test_dtm.py   12 unit tests exercising the engine directly.
demo.py             Narrative end-to-end script.
README.md           Quick start.  DOCS.md  Feature reference.  ARCHITECTURE.md  This.
```

**Dependency direction:** `web.py` → `core.py` and `cli.py` → `core.py`. Nothing in
`core.py` imports the front-ends. This keeps the engine testable in isolation (the
tests never touch the CLI or HTTP layers).

---

## 11. Extension points (where future work plugs in)

| Goal | Where it slots in |
|---|---|
| Revert a single row/change | new engine method reusing `_run` + `_as_of_by_pk` |
| Retention / compaction | a maintenance method over `_dtm_changes` |
| PostgreSQL / MySQL backend | swap engine+storage; attribution from session user |
| Branching / merging | new tables for branch refs + a merge algorithm over the log |
| Auth for web writes | add POST endpoints guarded by a token, calling `revert`/`tag` |

The layering means each of these touches one layer, not all three.

# How the Database Time Machine works

*A plain-English tour of "git for databases" — the idea, the mechanism, and the
interesting problems along the way.*

---

## The problem it solves

A normal database only remembers **now**. Run `UPDATE users SET plan='pro'` or
`DELETE FROM users WHERE plan='free'`, and the old values are gone forever. So three
everyday questions become impossible to answer after the fact:

- *Who changed this column, and why?*
- *Why did this data disappear after Tuesday's deployment?*
- *What did this table look like last Monday?*

Git answers exactly these questions — for code. The Database Time Machine brings the
same superpower to a database's **data and schema**.

---

## The one-sentence idea

> Turn every write into an immutable, attributed, queryable **event**, and keep them
> all. The current state is just the events replayed; the past is the events replayed
> up to a point in time.

That's the whole trick. Everything else — blame, time travel, revert, branching — is
a different question asked of the same event log.

---

## How a change gets captured

The engine installs three **triggers** on every tracked table: one each for
`INSERT`, `UPDATE`, and `DELETE`. Whenever a row changes, the trigger fires and writes
a record into an append-only log table (`_dtm_changes`) containing:

- the full row **before** and **after** the change (as JSON),
- **which** table and row,
- **who** made it and **why** (author + message),
- and **when**.

Because the capture happens *inside the database*, no application can sneak a change
past it — any write to a tracked table is recorded.

But SQLite has no notion of a logged-in user, so how does a trigger know *who*? A neat
trick: just before running your SQL, the tool writes the author and message into a
tiny one-row table (`_dtm_context`). The trigger reads from it. That little table is
the bridge that carries "who/why" from your command into the database's triggers.

```
  dtm exec --author alice -m "raise price"  "UPDATE ..."
        │  (1) write author/message into _dtm_context
        ▼
   your UPDATE runs ──trigger fires──▶ append {before, after, alice, "raise price", now}
                                        into _dtm_changes
```

---

## How time travel works

Reconstructing a table "as of last Monday" sounds like it needs a time machine. It
doesn't — it needs a query:

> For each row, find its most recent change **at or before** that moment. If that
> change was a delete, the row didn't exist; otherwise, its "after" snapshot *is* the
> row.

Run that for every row and you've rebuilt the table exactly as it was. The same
primitive powers **blame** (the latest change to one column), **row history** (all
changes to one row), and **diff** (compare two reconstructed states).

A subtle but important detail: timestamps are stored as ISO-8601 UTC strings, because
in that format *alphabetical* order equals *chronological* order — so "before this
moment" is a plain string comparison, no date math required.

---

## From "view the past" to "undo it"

Revert reconstructs the target state, compares it to the live table, and applies the
minimum inserts/updates/deletes to make them match — **through the same trigger path**,
so the revert is *itself* recorded. You can revert a revert. History is append-only,
just like git: you never rewrite the past, you add a new chapter that fixes it.

---

## The three problems that were actually interesting

**1. Proving the log wasn't secretly edited.** An audit trail is worthless if someone
can quietly rewrite it. So each change stores a hash of *itself plus the previous
change's hash* — a chain. Change any past record and its hash no longer matches, which
breaks every hash after it. `verify` recomputes the chain and points at the exact spot
someone tampered. The log becomes *tamper-evident*: not un-editable, but impossible to
edit **undetectably**.

**2. Branching a live database.** A branch is a fork of the whole database file that
shares history up to the fork point. Merging back is a genuine **three-way merge**: the
fork point is the common base, and for each row the tool compares base / ours / theirs.
Clean changes apply automatically; when both sides changed the same row, that's a
*conflict*, resolved by a chosen strategy (keep ours, take theirs, or take whichever is
newer).

**3. Making it fast.** Row-level triggers do real work, so a big batch was slow — until
switching SQLite to **WAL mode with `synchronous=NORMAL`**, the standard "fast but still
crash-safe" setting. A 2,000-change batch dropped from ~10 seconds to ~0.2 seconds, with
no loss of durability.

---

## Why it's built the way it is

- **Zero dependencies.** The engine, CLI, and web UI are pure Python standard library —
  no server, no `pip install`. It runs anywhere Python does. (Optional PostgreSQL and
  MySQL backends exist for real multi-user databases, where "who" comes from the
  authenticated session.)
- **One engine, many front-ends.** The CLI and the web UI are thin layers over the same
  `TimeMachine` class. The web UI added no new logic — it just exposes existing methods
  over HTTP.
- **Honest about limits.** Attribution on SQLite is cooperative; history only starts
  when you initialise tracking; `WITHOUT ROWID` tables are skipped rather than crashed
  on. These are documented, not hidden.

---

## In one breath

It's an event log with good questions asked of it. Capture every change with a trigger,
store the before/after with who-and-why, and *blame*, *time travel*, *diff*, *revert*,
*branch*, and *tamper-detection* all fall out of that one append-only log.

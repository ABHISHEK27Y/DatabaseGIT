"""
End-to-end demo of the Database Time Machine.

Runs the exact scenarios from the pitch:
    * "Who changed this column?"
    * "Why did user data disappear after Tuesday's deployment?"
    * "Show me the state of this table on <date>."

Run:  python demo.py
"""

import os
import time

from dtm.core import TimeMachine, _now

DB = "demo.sqlite"


def rule(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show(rows):
    if not rows:
        print("  (no rows)")
        return
    for r in rows:
        print("  ", dict(r) if not isinstance(r, dict) else r)


def main():
    if os.path.exists(DB):
        os.remove(DB)

    tm = TimeMachine(DB)
    tm.init_repo()

    rule("1. Monday -- alice creates the users table and adds two users")
    tm.exec_sql(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, email TEXT, plan TEXT)",
        author="alice", message="create users table",
    )
    tm.exec_sql(
        "INSERT INTO users(name, email, plan) VALUES('Ravi','ravi@x.com','free');"
        "INSERT INTO users(name, email, plan) VALUES('Meera','meera@x.com','pro');",
        author="alice", message="seed initial users",
    )
    show(tm.query("SELECT * FROM users"))

    time.sleep(0.01)
    monday_snapshot = _now()
    time.sleep(0.01)

    rule("2. Tuesday -- bob's deployment runs a bad migration that wipes free users")
    tm.exec_sql(
        "DELETE FROM users WHERE plan='free'",
        author="bob", message="deploy: cleanup script (BUG: too broad)",
    )
    tm.exec_sql(
        "UPDATE users SET plan='enterprise' WHERE name='Meera'",
        author="bob", message="deploy: upgrade Meera",
    )
    time.sleep(0.01)
    after_tuesday = _now()

    print("Live table now:")
    show(tm.query("SELECT * FROM users"))

    rule('3. "Show me the state of this table on Monday" (time travel)')
    show(tm.as_of("users", monday_snapshot))

    rule('3b. State right after Tuesday\'s deployment')
    show(tm.as_of("users", after_tuesday))

    rule('4. "Why did Ravi\'s data disappear?" -- full history of row 1')
    for change in tm.row_history("users", 1):
        print(f"  #{change['change_id']} {change['op']:<6} by "
              f"{change['author']:<6} @ {change['ts']}  -- {change['message']}")

    rule('5. "Who changed Meera\'s plan, and why?" -- blame')
    res = tm.blame("users", 2, "plan")
    print(f"  {res['old_value']!r} -> {res['new_value']!r}")
    print(f"  by {res['author']} : {res['message']!r}  @ {res['ts']}")

    rule("6. Diff: what changed to the users table across Tuesday's deploy")
    d = tm.diff("users", monday_snapshot, after_tuesday)
    print("  added   :", d["added"])
    print("  removed :", d["removed"])
    print("  changed :", d["changed"])

    rule("7. Schema history -- who added a column later")
    tm.exec_sql(
        "ALTER TABLE users ADD COLUMN signup_date TEXT",
        author="carol", message="track signup dates",
    )
    for s in tm.schema_log():
        print(f"  schema #{s['schema_id']} by {s['author']:<6} @ {s['ts']}"
              f"  -- {s['message']}")
    print("\n  schema-blame users.signup_date:")
    print("  ", tm.schema_blame("users", "signup_date"))

    rule("8. Named checkpoint (tag) + one-command REVERT of the bad deploy")
    tm.tag("before-cleanup", author="alice", message="known-good point",
           at=monday_snapshot)
    print("  tagged 'before-cleanup' at Monday's state")
    print("  live table before revert:")
    show(tm.query("SELECT * FROM users"))
    summary = tm.revert("users", "before-cleanup", author="carol",
                        message="restore users after bad deploy")
    print(f"  revert applied: {summary}")
    print("  live table AFTER revert (Ravi is back, Meera restored):")
    show(tm.query("SELECT * FROM users"))

    rule("9. Stats / dashboard data")
    s = tm.stats()
    print(f"  total changes : {s['total_changes']}")
    print(f"  by operation  : {s['by_op']}")
    print(f"  by author     : "
          + ", ".join(f"{a['author']}={a['n']}" for a in s['by_author']))

    tm.close()
    print(f"\nDone. Explore visually with:  python -m dtm serve {DB}")


if __name__ == "__main__":
    main()

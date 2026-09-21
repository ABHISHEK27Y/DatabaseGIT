"""Tests for the Database Time Machine engine."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dtm.core import TimeMachine  # noqa: E402


class TimeMachineTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.path)  # start clean
        self.tm = TimeMachine(self.path)
        self.tm.init_repo()

    def tearDown(self):
        self.tm.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _ts(self):
        # ensure strictly increasing timestamps between operations
        time.sleep(0.005)
        from dtm.core import _now
        t = _now()
        time.sleep(0.005)
        return t

    def test_insert_update_delete_are_logged(self):
        self.tm.exec_sql(
            "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)",
            author="alice", message="create",
        )
        self.tm.exec_sql(
            "INSERT INTO products(name, price) VALUES('Widget', 9.99)",
            author="alice", message="add widget",
        )
        self.tm.exec_sql(
            "UPDATE products SET price = 12.50 WHERE name='Widget'",
            author="bob", message="raise price",
        )
        log = self.tm.log(table="products")
        ops = sorted(r["op"] for r in log)
        self.assertEqual(ops, ["INSERT", "UPDATE"])

    def test_blame_reports_last_author(self):
        self.tm.exec_sql(
            "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)",
            author="alice", message="create",
        )
        self.tm.exec_sql(
            "INSERT INTO products(name, price) VALUES('Widget', 9.99)",
            author="alice", message="add widget",
        )
        self.tm.exec_sql(
            "UPDATE products SET price = 12.50 WHERE id=1",
            author="bob", message="raise price",
        )
        res = self.tm.blame("products", 1, "price")
        self.assertIsNotNone(res)
        self.assertEqual(res["author"], "bob")
        self.assertEqual(res["old_value"], 9.99)
        self.assertEqual(res["new_value"], 12.5)

    def test_time_travel_as_of(self):
        self.tm.exec_sql(
            "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)",
            author="alice", message="create",
        )
        self.tm.exec_sql(
            "INSERT INTO products(name, price) VALUES('Widget', 9.99)",
            author="alice", message="add",
        )
        t_before_raise = self._ts()
        self.tm.exec_sql(
            "UPDATE products SET price = 20.0 WHERE id=1",
            author="bob", message="raise",
        )
        t_after = self._ts()

        old_state = self.tm.as_of("products", t_before_raise)
        self.assertEqual(len(old_state), 1)
        self.assertEqual(old_state[0]["price"], 9.99)

        new_state = self.tm.as_of("products", t_after)
        self.assertEqual(new_state[0]["price"], 20.0)

    def test_deleted_row_absent_in_later_snapshot(self):
        self.tm.exec_sql(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)",
            author="a", message="create",
        )
        self.tm.exec_sql("INSERT INTO t(v) VALUES('x')", author="a", message="add")
        t_mid = self._ts()
        self.tm.exec_sql("DELETE FROM t WHERE id=1", author="a", message="del")
        t_end = self._ts()
        self.assertEqual(len(self.tm.as_of("t", t_mid)), 1)
        self.assertEqual(len(self.tm.as_of("t", t_end)), 0)

    def test_baseline_of_preexisting_rows(self):
        # Rows created before tracking a table (in the same exec that creates it)
        # should still be reconstructable.
        self.tm.exec_sql(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"
            "INSERT INTO t(v) VALUES('seed1');"
            "INSERT INTO t(v) VALUES('seed2');",
            author="a", message="seed",
        )
        now = self._ts()
        state = self.tm.as_of("t", now)
        self.assertEqual(len(state), 2)

    def test_diff_between_two_times(self):
        self.tm.exec_sql(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)",
            author="a", message="create",
        )
        self.tm.exec_sql("INSERT INTO t(v) VALUES('a')", author="a", message="add a")
        t1 = self._ts()
        self.tm.exec_sql("INSERT INTO t(v) VALUES('b')", author="a", message="add b")
        self.tm.exec_sql("UPDATE t SET v='A' WHERE id=1", author="a", message="edit")
        t2 = self._ts()
        d = self.tm.diff("t", t1, t2)
        self.assertEqual(len(d["added"]), 1)
        self.assertEqual(len(d["changed"]), 1)
        self.assertEqual(len(d["removed"]), 0)

    def test_revert_restores_past_state(self):
        self.tm.exec_sql(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, plan TEXT)",
            author="alice", message="create",
        )
        self.tm.exec_sql(
            "INSERT INTO users(name, plan) VALUES('Ravi','free');"
            "INSERT INTO users(name, plan) VALUES('Meera','pro');",
            author="alice", message="seed",
        )
        good = self._ts()
        # a bad deploy: delete a user and change another
        self.tm.exec_sql("DELETE FROM users WHERE name='Ravi'", author="bob", message="oops")
        self.tm.exec_sql("UPDATE users SET plan='x' WHERE name='Meera'", author="bob", message="oops2")
        self.assertEqual(len(self.tm.query("SELECT * FROM users")), 1)

        summary = self.tm.revert("users", good, author="carol", message="restore")
        self.assertEqual(summary["inserted"], 1)   # Ravi comes back
        self.assertEqual(summary["updated"], 1)     # Meera restored
        live = {r["name"]: r["plan"] for r in self.tm.query("SELECT * FROM users")}
        self.assertEqual(live, {"Ravi": "free", "Meera": "pro"})

    def test_revert_is_itself_recorded(self):
        self.tm.exec_sql("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO t(v) VALUES('x')", author="a", message="add")
        good = self._ts()
        self.tm.exec_sql("DELETE FROM t WHERE id=1", author="a", message="del")
        before = len(self.tm.log(table="t", limit=1000))
        self.tm.revert("t", good, author="a", message="restore")
        after = len(self.tm.log(table="t", limit=1000))
        self.assertGreater(after, before)  # revert added change rows

    def test_tags_and_resolution(self):
        self.tm.exec_sql("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO t(v) VALUES('one')", author="a", message="add")
        self.tm.tag("v1", author="a", message="first release")
        self.tm.exec_sql("UPDATE t SET v='two' WHERE id=1", author="a", message="edit")

        ts = self.tm.resolve_time("v1")
        self.assertTrue(ts and ts != "v1")  # resolved to a real timestamp
        state = self.tm.as_of("t", ts)
        self.assertEqual(state[0]["v"], "one")
        # unknown spec passes through unchanged
        self.assertEqual(self.tm.resolve_time("2020-01-01T00:00:00+00:00"),
                         "2020-01-01T00:00:00+00:00")

    def test_revert_by_tag(self):
        self.tm.exec_sql("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO t(v) VALUES('keep')", author="a", message="add")
        self.tm.tag("safe", author="a")
        self.tm.exec_sql("DELETE FROM t", author="a", message="wipe")
        self.assertEqual(len(self.tm.query("SELECT * FROM t")), 0)
        self.tm.revert("t", "safe", author="a", message="restore from tag")
        self.assertEqual(len(self.tm.query("SELECT * FROM t")), 1)

    def test_stats(self):
        self.tm.exec_sql("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)", author="alice", message="c")
        self.tm.exec_sql("INSERT INTO t(v) VALUES('a')", author="alice", message="add")
        self.tm.exec_sql("UPDATE t SET v='b' WHERE id=1", author="bob", message="edit")
        s = self.tm.stats()
        self.assertEqual(s["total_changes"], 2)  # 1 insert + 1 update
        self.assertEqual(s["by_op"].get("UPDATE"), 1)
        authors = {a["author"] for a in s["by_author"]}
        self.assertEqual(authors, {"alice", "bob"})

    def test_hash_chain_detects_tampering(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(v) VALUES('x')", author="a", message="add")
        self.tm.exec_sql("UPDATE p SET v='y' WHERE id=1", author="a", message="edit")
        self.assertTrue(self.tm.verify_integrity()["ok"])
        # secretly rewrite history
        self.tm.conn.execute("UPDATE _dtm_changes SET new_json='{\"id\":1,\"v\":\"EVIL\"}' WHERE change_id=1")
        self.tm.conn.commit()
        res = self.tm.verify_integrity()
        self.assertFalse(res["ok"])
        self.assertEqual(res["broken_at"], 1)

    def test_session_api(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v INT)", author="a", message="c")
        with self.tm.session(author="api", message="bulk") as cur:
            cur.execute("INSERT INTO p(v) VALUES(1)")
            cur.execute("INSERT INTO p(v) VALUES(2)")
            cur.execute("UPDATE p SET v=99 WHERE id=1")
        self.assertEqual(len(self.tm.query("SELECT * FROM p")), 2)
        authors = {c["author"] for c in self.tm.log(table="p", limit=100)}
        self.assertEqual(authors, {"api"})

    def test_anomalies(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v INT)", author="a", message="c")
        self.tm.exec_sql("".join(f"INSERT INTO p(v) VALUES({i});" for i in range(8)),
                         author="a", message="seed")
        self.tm.exec_sql("DELETE FROM p", author="bad", message="wipe everything")
        flags = self.tm.anomalies(threshold=5)
        self.assertTrue(any(f["op"] == "DELETE" and f["rows_affected"] == 8 for f in flags))

    def test_log_filters(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v TEXT)", author="alice", message="c")
        self.tm.exec_sql("INSERT INTO p(v) VALUES('a')", author="alice", message="add a")
        self.tm.exec_sql("UPDATE p SET v='b' WHERE id=1", author="bob", message="edit")
        self.assertEqual(len(self.tm.log(table="p", author="bob", limit=100)), 1)
        self.assertEqual(len(self.tm.log(table="p", op="INSERT", limit=100)), 1)
        self.assertEqual(len(self.tm.log(table="p", contains="edit", limit=100)), 1)

    def test_diff_reports_changed_fields(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, a TEXT, b TEXT)", author="x", message="c")
        self.tm.exec_sql("INSERT INTO p(a,b) VALUES('1','1')", author="x", message="add")
        t1 = self._ts()
        self.tm.exec_sql("UPDATE p SET a='2' WHERE id=1", author="x", message="edit a")
        t2 = self._ts()
        d = self.tm.diff("p", t1, t2)
        self.assertEqual(d["changed"][0]["fields"], ["a"])

    def test_branch_and_merge(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, name TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(name) VALUES('orig')", author="a", message="add")
        path = self.tm.branch("feature", author="a")
        self._tmp_extra = path
        # change on the branch
        fb = TimeMachine(path)
        fb.exec_sql("UPDATE p SET name='changed' WHERE id=1", author="dev", message="rename")
        fb.close()
        res = self.tm.merge("feature", author="a")
        self.assertEqual(res["applied"], 1)
        self.assertEqual(len(res["conflicts"]), 0)
        self.assertEqual(self.tm.query("SELECT name FROM p WHERE id=1")[0]["name"], "changed")
        os.remove(path)

    def test_merge_detects_conflict(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, name TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(name) VALUES('base')", author="a", message="add")
        path = self.tm.branch("feature", author="a")
        fb = TimeMachine(path)
        fb.exec_sql("UPDATE p SET name='theirs' WHERE id=1", author="dev", message="branch edit")
        fb.close()
        # both sides change the same row differently
        self.tm.exec_sql("UPDATE p SET name='ours' WHERE id=1", author="a", message="main edit")
        res = self.tm.merge("feature", author="a")
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertEqual(self.tm.query("SELECT name FROM p WHERE id=1")[0]["name"], "ours")
        os.remove(path)

    def test_merge_strategy_theirs(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, name TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(name) VALUES('base')", author="a", message="add")
        path = self.tm.branch("f", author="a")
        fb = TimeMachine(path)
        fb.exec_sql("UPDATE p SET name='theirs' WHERE id=1", author="dev", message="branch")
        fb.close()
        self.tm.exec_sql("UPDATE p SET name='ours' WHERE id=1", author="a", message="main")
        res = self.tm.merge("f", author="a", strategy="theirs")
        self.assertEqual(len(res["conflicts"]), 0)
        self.assertEqual(len(res["resolved"]), 1)
        self.assertEqual(self.tm.query("SELECT name FROM p WHERE id=1")[0]["name"], "theirs")
        os.remove(path)

    def test_merge_strategy_newest(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, name TEXT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(name) VALUES('base')", author="a", message="add")
        path = self.tm.branch("f", author="a")
        fb = TimeMachine(path)
        fb.exec_sql("UPDATE p SET name='theirs' WHERE id=1", author="dev", message="branch")
        fb.close()
        self._ts()  # ensure our change is strictly newer
        self.tm.exec_sql("UPDATE p SET name='ours' WHERE id=1", author="a", message="main")
        res = self.tm.merge("f", author="a", strategy="newest")
        # ours is newer -> ours kept
        self.assertEqual(self.tm.query("SELECT name FROM p WHERE id=1")[0]["name"], "ours")
        self.assertEqual(len(res["conflicts"]), 0)
        os.remove(path)

    def test_compaction_preserves_time_travel_after_cutoff(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v INT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(v) VALUES(1)", author="a", message="add")
        self.tm.exec_sql("UPDATE p SET v=2 WHERE id=1", author="a", message="e1")
        cutoff = self._ts()
        self.tm.exec_sql("UPDATE p SET v=3 WHERE id=1", author="a", message="e2")
        after = self._ts()

        before_count = len(self.tm.log(table="p", limit=1000))
        res = self.tm.compact(cutoff)
        self.assertGreater(res["removed"], 0)
        after_count = len(self.tm.log(table="p", limit=1000))
        self.assertLess(after_count, before_count)  # history shrank

        # state at cutoff and after are still correct
        self.assertEqual(self.tm.as_of("p", cutoff)[0]["v"], 2)
        self.assertEqual(self.tm.as_of("p", after)[0]["v"], 3)
        self.assertEqual(self.tm.query("SELECT v FROM p WHERE id=1")[0]["v"], 3)

    def test_compaction_keeps_hash_chain_valid(self):
        self.tm.exec_sql("CREATE TABLE p(id INTEGER PRIMARY KEY, v INT)", author="a", message="c")
        self.tm.exec_sql("INSERT INTO p(v) VALUES(1)", author="a", message="add")
        cutoff = self._ts()
        self.tm.exec_sql("UPDATE p SET v=2 WHERE id=1", author="a", message="e")
        self.tm.compact(cutoff)
        self.assertTrue(self.tm.verify_integrity()["ok"])

    def test_schema_blame(self):
        self.tm.exec_sql(
            "CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT)",
            author="alice", message="create",
        )
        self.tm.exec_sql(
            "ALTER TABLE t ADD COLUMN email TEXT",
            author="carol", message="add email column",
        )
        res = self.tm.schema_blame("t", "email")
        self.assertIsNotNone(res)
        self.assertEqual(res["author"], "carol")
        self.assertIsNotNone(res["definition"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

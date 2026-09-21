# 2-Minute Demo Script

A shot-by-shot script for recording a short walkthrough video/GIF. Record your screen
(OBS Studio, ScreenToGif, or the Windows Game Bar `Win+G`), read the narration, and run
the commands. Total runtime ~2 minutes.

**Before you start:** open a terminal in the project folder, and delete any old demo DB:

```bash
rm -f demo.sqlite demo.sqlite-wal demo.sqlite-shm
```

---

### Shot 1 — The hook  (0:00–0:15)

> *"A normal database only remembers the present. Change a row, and the old value is
> gone. This tool gives any SQLite database a full, git-style history — so you can see
> who changed what, travel back in time, and even undo a bad deployment."*

Show the README banner or just the terminal.

---

### Shot 2 — Set up a database  (0:15–0:35)

Run these and let them scroll:

```bash
python -m dtm init shop.sqlite
python -m dtm exec shop.sqlite -a alice -m "create products" "CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, price REAL)"
python -m dtm exec shop.sqlite -a alice -m "add widget" "INSERT INTO products(name,price) VALUES('Widget', 9.99)"
python -m dtm exec shop.sqlite -a bob   -m "raise price" "UPDATE products SET price=12.50 WHERE id=1"
```

> *"Every change goes through the tool with an author and a message — like a commit."*

---

### Shot 3 — Who changed this, and why?  (0:35–0:55)

```bash
python -m dtm blame shop.sqlite products 1 price
```

> *"Blame tells me the price went from 9.99 to 12.50, changed by bob, with his reason —
> instantly."*

---

### Shot 4 — The bad deployment + time travel  (0:55–1:20)

```bash
python -m dtm tag shop.sqlite before-deploy -m "known good"
python -m dtm exec shop.sqlite -a deploy -m "cleanup (BUG)" "DELETE FROM products"
python -m dtm query shop.sqlite "SELECT * FROM products"
```

> *"A deployment just wiped the table. In a normal database, that data is gone. Here…"*

```bash
python -m dtm as-of shop.sqlite products --at before-deploy
```

> *"…I can see exactly what it looked like before the deploy."*

---

### Shot 5 — One-command undo  (1:20–1:40)

```bash
python -m dtm revert shop.sqlite products --to before-deploy -a alice -m "restore"
python -m dtm query shop.sqlite "SELECT * FROM products"
```

> *"And I can restore it with one command — and the restore is itself recorded."*

---

### Shot 6 — Tamper-evidence  (1:40–1:55)

```bash
python -m dtm verify shop.sqlite
```

> *"The whole audit log is hash-chained, so I can prove it was never secretly edited."*

---

### Shot 7 — The web UI  (1:55–2:10)

```bash
python -m dtm serve shop.sqlite
```

Open `http://127.0.0.1:8080`. Click **Timeline** (show the git-graph), then
**Attribution**, then **Integrity**.

> *"And there's a zero-dependency web UI to explore all of it visually — the timeline,
> blame, and the integrity check. Git for databases."*

---

### Cleanup

```bash
rm -f shop.sqlite shop.sqlite-wal shop.sqlite-shm
```

**Tip for a GIF:** record just Shots 2–5 (setup → bad deploy → time travel → undo).
That's the strongest 40-second story and loops well.

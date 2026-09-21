"""
Audit-report export for the Database Time Machine.

Turns the change log into a shareable report (HTML or CSV) -- the "who changed
what, and when" document an auditor or manager would ask for.
"""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime, timezone

from .core import TimeMachine


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def report_csv(tm: TimeMachine, **filters) -> str:
    rows = tm.report_rows(**filters)
    buf = io.StringIO()
    cols = ["change_id", "ts", "tbl", "pk", "op", "author", "message",
            "old_json", "new_json"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c) for c in cols})
    return buf.getvalue()


def report_html(tm: TimeMachine, title: str = "Audit Report", **filters) -> str:
    rows = tm.report_rows(**filters)
    stats = tm.stats()
    integrity = tm.verify_integrity()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")

    badge = ('<span class="ok">✓ verified — tamper-evident log intact</span>'
             if integrity["ok"]
             else f'<span class="bad">✗ integrity broken at change '
                  f'#{integrity.get("broken_at")}</span>')

    body = []
    for r in rows:
        op = _esc(r.get("op"))
        body.append(
            f"<tr><td class=mono>{_esc(r.get('change_id'))}</td>"
            f"<td class=mono>{_esc(r.get('ts'))}</td>"
            f"<td>{_esc(r.get('tbl'))}</td><td class=mono>{_esc(r.get('pk'))}</td>"
            f"<td><span class='op {op}'>{op}</span></td>"
            f"<td>{_esc(r.get('author'))}</td><td>{_esc(r.get('message'))}</td>"
            f"<td class=mono>{_esc(r.get('old_json'))}</td>"
            f"<td class=mono>{_esc(r.get('new_json'))}</td></tr>"
        )

    by_author = ", ".join(f"{_esc(a['author'])} ({a['n']})"
                          for a in stats["by_author"]) or "—"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{_esc(title)}</title>
<style>
  body{{font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif; color:#1c1b18;
    background:#f6f4ee; margin:0; padding:40px;}}
  h1{{font-family:Georgia,serif; font-size:26px; margin:0 0 4px;}}
  .meta{{color:#7b786f; font-size:13px; margin-bottom:20px;}}
  .ok{{color:#1a5f4a; font-weight:600;}} .bad{{color:#a23b3b; font-weight:600;}}
  .cards{{display:flex; gap:24px; margin:20px 0; flex-wrap:wrap;}}
  .card{{background:#fff; border:1px solid #e6e2d7; border-radius:10px; padding:14px 20px;}}
  .card b{{font-family:Georgia,serif; font-size:24px; display:block;}}
  .card span{{color:#7b786f; font-size:11px; text-transform:uppercase; letter-spacing:.08em;}}
  table{{border-collapse:collapse; width:100%; background:#fff; border:1px solid #e6e2d7;
    border-radius:10px; overflow:hidden; font-size:12.5px;}}
  th{{background:#efece3; text-align:left; padding:10px 12px; font-size:10.5px;
    text-transform:uppercase; letter-spacing:.06em; color:#7b786f;}}
  td{{padding:9px 12px; border-top:1px solid #efece3; vertical-align:top;}}
  .mono{{font-family:ui-monospace,Consolas,monospace; font-size:11.5px; color:#413f39;}}
  .op{{font-weight:700; font-size:11px;}} .op.INSERT{{color:#2f7d5b;}}
  .op.UPDATE{{color:#916516;}} .op.DELETE{{color:#a23b3b;}}
</style></head><body>
  <h1>{_esc(title)}</h1>
  <div class="meta">Generated {generated} · {badge}</div>
  <div class="cards">
    <div class="card"><b>{stats['total_changes']}</b><span>Total changes</span></div>
    <div class="card"><b>{stats['total_txns']}</b><span>Commits</span></div>
    <div class="card"><b>{stats['tables_tracked']}</b><span>Tables</span></div>
    <div class="card"><b>{len(rows)}</b><span>Rows in report</span></div>
  </div>
  <div class="meta">Changes by author: {by_author}</div>
  <table><thead><tr>
    <th>#</th><th>Timestamp</th><th>Table</th><th>Row</th><th>Op</th>
    <th>Author</th><th>Message</th><th>Before</th><th>After</th>
  </tr></thead><tbody>
  {''.join(body) or '<tr><td colspan=9>No changes.</td></tr>'}
  </tbody></table>
</body></html>"""


def write_report(tm: TimeMachine, out_path: str, fmt: str = "html", **filters) -> None:
    content = report_html(tm, **filters) if fmt == "html" else report_csv(tm, **filters)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(content)

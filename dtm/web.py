"""
Zero-dependency web UI for the Database Time Machine.

Built entirely on the Python standard library (``http.server``) so the project's
"no pip install, runs anywhere" promise holds even for the UI. It exposes a small
JSON API and serves a single-page app that lets you browse the timeline, travel
through time, diff, blame, and view stats visually.

Launch with:  python -m dtm serve mydb.sqlite --port 8080
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import TimeMachine


def _api(tm: TimeMachine, path: str, q: dict) -> object:
    """Route an /api/* path to the engine and return JSON-able data."""
    one = lambda k, d=None: (q.get(k, [d])[0])  # noqa: E731

    if path == "/api/tables":
        return tm.user_tables()
    if path == "/api/columns":
        return tm.columns(one("table"))
    if path == "/api/stats":
        return tm.stats()
    if path == "/api/tags":
        return tm.list_tags()
    if path == "/api/schema-log":
        return tm.schema_log()
    if path == "/api/history":
        return tm.row_history(one("table"), int(one("pk")))
    if path == "/api/asof":
        return tm.as_of(one("table"), tm.resolve_time(one("at")))
    if path == "/api/diff":
        return tm.diff(
            one("table"), tm.resolve_time(one("from")), tm.resolve_time(one("to"))
        )
    if path == "/api/blame":
        return tm.blame(one("table"), int(one("pk")), one("column"))
    if path == "/api/verify":
        return tm.verify_integrity()
    if path == "/api/anomalies":
        return tm.anomalies(threshold=int(one("threshold", "5")))
    if path == "/api/branches":
        return tm.list_branches()
    if path == "/api/log":
        return tm.log(
            table=one("table") or None, limit=int(one("limit", "50")),
            author=one("author") or None, op=one("op") or None,
            contains=one("contains") or None,
        )
    raise KeyError(path)


def _api_post(tm: TimeMachine, path: str, data: dict) -> object:
    """Handle write endpoints (currently: revert)."""
    if path == "/api/revert":
        table = data.get("table")
        at = tm.resolve_time(data.get("at"))
        author = data.get("author") or "web-ui"
        message = data.get("message") or f"revert {table} via web UI"
        summary = tm.revert(table, at, author=author, message=message)
        return {"ok": True, "summary": summary}
    raise KeyError(path)


def make_handler(db_path: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quieter console
            pass

        def _send(self, code, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/api/"):
                # Fresh connection per request -> thread-safe with ThreadingHTTPServer.
                tm = TimeMachine(db_path)
                try:
                    result = _api(tm, parsed.path, parse_qs(parsed.query))
                    self._send(
                        200, json.dumps(result, default=str),
                        "application/json; charset=utf-8",
                    )
                except KeyError:
                    self._send(404, '{"error":"not found"}', "application/json")
                except Exception as exc:  # surface engine errors to the UI
                    self._send(
                        400, json.dumps({"error": str(exc)}),
                        "application/json; charset=utf-8",
                    )
                finally:
                    tm.close()
                return
            self._send(404, "not found", "text/plain")

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                data = {}
            tm = TimeMachine(db_path)
            try:
                result = _api_post(tm, parsed.path, data)
                self._send(200, json.dumps(result, default=str),
                           "application/json; charset=utf-8")
            except KeyError:
                self._send(404, '{"error":"not found"}', "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"error": str(exc)}),
                           "application/json; charset=utf-8")
            finally:
                tm.close()

    return Handler


def serve(tm: TimeMachine, host: str = "127.0.0.1", port: int = 8080) -> None:
    db_path = tm.path
    tm.close()  # each request opens its own connection
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path))
    url = f"http://{host}:{port}"
    print(f"Database Time Machine web UI running at {url}")
    print(f"  database: {db_path}")
    print("  press Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.server_close()


# --------------------------------------------------------------------------- #
# Single-page app  --  refined "ledger" theme.
# Editorial, restrained: warm paper, charcoal ink, one deep-green accent, serif
# headings, hairline rules, generous whitespace. No external assets; offline.
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Time Machine</title>
<style>
  :root{
    --paper:#f6f4ee; --card:#ffffff; --ink:#1c1b18; --ink2:#413f39; --muted:#7b786f;
    --faint:#a7a399; --line:#e6e2d7; --line2:#efece3;
    --accent:#1a5f4a; --accent-ink:#12463699; --accent-soft:#e9f1ec;
    --ins:#2f7d5b; --ins-bg:#eaf3ee; --upd:#916516; --upd-bg:#f6efe0; --del:#a23b3b; --del-bg:#f6eaea;
    --shadow:0 1px 2px rgba(28,27,24,.04), 0 12px 30px -18px rgba(28,27,24,.20);
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
    --mono:ui-monospace,"SF Mono","Cascadia Mono",Consolas,monospace;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      --paper:#16171a; --card:#1d1e22; --ink:#eceae4; --ink2:#c7c4bc; --muted:#928f86;
      --faint:#6a675f; --line:#2b2c31; --line2:#26272b;
      --accent:#67c3a1; --accent-ink:#67c3a199; --accent-soft:#1c3a31;
      --ins:#69c39a; --ins-bg:#1a3229; --upd:#d1a24e; --upd-bg:#332a18; --del:#e0797d; --del-bg:#35232400;
      --shadow:0 1px 2px rgba(0,0,0,.3), 0 14px 34px -18px rgba(0,0,0,.6);
    }
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{margin:0; color:var(--ink); background:var(--paper); font:15px/1.6 var(--sans);
    -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;}
  .mono{font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:.86em;}
  .serif{font-family:var(--serif);}

  header{display:flex; align-items:center; gap:14px; padding:20px 34px; border-bottom:1px solid var(--line);}
  .mark{width:34px; height:34px; border-radius:8px; border:1px solid var(--accent); color:var(--accent);
    display:grid; place-items:center; font-family:var(--serif); font-size:19px; font-weight:600;}
  .wm{font-family:var(--serif); font-size:19px; font-weight:600; letter-spacing:.01em;}
  .wm small{display:block; font-family:var(--sans); font-size:11px; letter-spacing:.14em; text-transform:uppercase;
    color:var(--muted); font-weight:500; margin-top:1px;}
  .spacer{flex:1;}
  .dbpill{font-size:12px; color:var(--muted); border:1px solid var(--line); border-radius:999px;
    padding:6px 14px; letter-spacing:.02em; background:var(--card);}
  .dbpill b{color:var(--ink); font-weight:600;}

  .layout{display:flex; min-height:calc(100vh - 75px);}
  nav{width:214px; flex:none; padding:26px 16px; border-right:1px solid var(--line);}
  .nlabel{font-size:10.5px; letter-spacing:.16em; color:var(--faint); text-transform:uppercase; padding:0 12px 14px;}
  nav button{display:block; width:100%; text-align:left; cursor:pointer; background:none; border:none;
    color:var(--ink2); padding:9px 13px; border-radius:8px; font:500 14.5px var(--sans); margin-bottom:3px;
    transition:background .15s,color .15s; letter-spacing:.01em;}
  nav button:hover{background:var(--line2); color:var(--ink);}
  nav button.active{color:var(--accent); font-weight:600; background:var(--accent-soft);
    box-shadow:inset 2px 0 0 var(--accent);}

  main{flex:1; padding:34px 44px; overflow:auto; min-width:0; max-width:1180px;}
  .vh{font-family:var(--serif); font-size:30px; font-weight:600; letter-spacing:-.01em; margin:0 0 4px;}
  .vsub{color:var(--muted); margin:0 0 30px; font-size:14.5px;}
  .fade{animation:fade .3s ease both;} @keyframes fade{from{opacity:0; transform:translateY(6px);}to{opacity:1;}}

  /* stat cards */
  .cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
    background:var(--line); border:1px solid var(--line); border-radius:12px; overflow:hidden; margin-bottom:30px;}
  .st{background:var(--card); padding:20px 22px;}
  .st .num{font-family:var(--serif); font-size:38px; font-weight:600; line-height:1; letter-spacing:-.02em;}
  .st.key .num{color:var(--accent);}
  .st .lab{margin-top:9px; font-size:11px; letter-spacing:.11em; text-transform:uppercase; color:var(--muted);}

  .panel{background:var(--card); border:1px solid var(--line); border-radius:12px; padding:22px 24px;
    box-shadow:var(--shadow); margin-bottom:22px;}
  .panel h3{margin:0 0 4px; font-family:var(--serif); font-size:16px; font-weight:600;}
  .panel .cap{color:var(--muted); font-size:12.5px; margin:0 0 18px;}
  .grid2{display:grid; grid-template-columns:1fr 1fr; gap:22px;}
  @media(max-width:900px){.grid2{grid-template-columns:1fr;}}

  /* bars */
  .barrow{display:grid; grid-template-columns:130px 1fr 40px; align-items:center; gap:14px; margin:13px 0;}
  .barrow .k{color:var(--ink2); font-size:13.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .track{height:6px; border-radius:3px; background:var(--line2);}
  .fill{height:100%; width:0; border-radius:3px; background:var(--accent); opacity:.85; transition:width .8s cubic-bezier(.2,.8,.2,1);}
  .barrow .v{text-align:right; font-family:var(--mono); font-size:13px; color:var(--ink2);}

  /* tables */
  .tblwrap{border:1px solid var(--line); border-radius:12px; overflow:hidden;}
  table{border-collapse:collapse; width:100%; font-size:13.5px; background:var(--card);}
  thead th{text-align:left; padding:12px 16px; font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
    color:var(--muted); font-weight:600; border-bottom:1px solid var(--line); background:var(--line2);}
  tbody td{padding:12px 16px; border-bottom:1px solid var(--line2); white-space:nowrap; color:var(--ink2);}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr:hover{background:var(--line2);}
  .tag{display:inline-flex; align-items:center; gap:6px; padding:2px 9px; border-radius:5px; font-size:11px;
    font-weight:600; letter-spacing:.03em;}
  .tag::before{content:""; width:5px; height:5px; border-radius:50%; background:currentColor;}
  .tag.INSERT{color:var(--ins); background:var(--ins-bg);}
  .tag.UPDATE{color:var(--upd); background:var(--upd-bg);}
  .tag.DELETE{color:var(--del); background:var(--del-bg);}

  /* timeline (restrained) */
  .glog{position:relative;}
  .gnode{display:grid; grid-template-columns:22px 1fr; gap:16px;}
  .rail{position:relative;}
  .rail::before{content:""; position:absolute; left:5px; top:0; bottom:0; width:1px; background:var(--line);}
  .gnode:first-child .rail::before{top:20px;}
  .gnode:last-child .rail::before{height:20px;}
  .node{position:absolute; left:0; top:14px; width:11px; height:11px; border-radius:50%; background:var(--card);
    border:2px solid var(--faint); z-index:2;}
  .gnode.INSERT .node{border-color:var(--ins);} .gnode.UPDATE .node{border-color:var(--upd);}
  .gnode.DELETE .node{border-color:var(--del);}
  .gcard{border-bottom:1px solid var(--line2); padding:11px 0 18px;}
  .gtop{display:flex; align-items:baseline; gap:11px; flex-wrap:wrap;}
  .ghash{font-family:var(--mono); font-size:12px; color:var(--muted);}
  .gtbl{color:var(--muted); font-size:12.5px;}
  .gts{margin-left:auto; color:var(--faint); font-size:11.5px;}
  .gmsg{margin-top:6px; font-size:14.5px; color:var(--ink);}
  .gauth{margin-top:3px; font-size:12.5px; color:var(--muted);}
  .gauth b{color:var(--ink2); font-weight:600;}

  /* controls */
  .row{display:flex; gap:14px; flex-wrap:wrap; align-items:flex-end; margin-bottom:24px;}
  label{display:block; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); margin-bottom:7px;}
  select,input{background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:8px;
    padding:9px 12px; font:14px var(--sans); outline:none; transition:border-color .15s,box-shadow .15s;}
  select:focus,input:focus{border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft);}
  button.go{background:var(--accent); color:#fff; border:none; border-radius:8px; padding:10px 20px;
    font:600 14px var(--sans); cursor:pointer; transition:filter .15s;}
  button.go:hover{filter:brightness(1.08);}

  /* diff */
  .diffbox{display:grid; grid-template-columns:1fr 1fr 1fr; gap:20px;}
  @media(max-width:900px){.diffbox{grid-template-columns:1fr;}}
  .dcol h4{margin:0 0 12px; font-size:12px; letter-spacing:.08em; text-transform:uppercase; display:flex; gap:9px; align-items:center;}
  .cnt{font-family:var(--mono); font-size:11px; color:var(--muted); border:1px solid var(--line); border-radius:999px; padding:1px 8px;}
  .dcol.add h4{color:var(--ins);} .dcol.rem h4{color:var(--del);} .dcol.chg h4{color:var(--upd);}
  .dentry{font-size:12.5px; padding:10px 12px; border:1px solid var(--line); border-radius:8px; margin-bottom:9px; word-break:break-word; background:var(--card);}
  .rem-t{color:var(--del);} .add-t{color:var(--ins);}

  /* blame */
  .blamecard{display:flex; gap:18px; align-items:flex-start;}
  .avatar{width:46px; height:46px; border-radius:50%; flex:none; display:grid; place-items:center; font-weight:600;
    font-family:var(--serif); font-size:20px; color:var(--accent); background:var(--accent-soft); border:1px solid var(--line);}
  .chip{display:inline-block; padding:3px 11px; border-radius:6px; font-size:13px; border:1px solid var(--line);}
  .chip.old{color:var(--del); background:var(--del-bg);} .chip.new{color:var(--ins); background:var(--ins-bg);}
  .arrow{color:var(--faint); margin:0 10px;}

  .empty{color:var(--faint); padding:44px; text-align:center; font-size:14px;}
  .err{color:var(--del); background:var(--del-bg); border:1px solid var(--del); border-radius:9px; padding:13px 16px; font-size:14px;}
  .hint{color:var(--muted); font-size:13px;}
  code{background:var(--line2); padding:2px 7px; border-radius:5px; font-family:var(--mono); font-size:12.5px; color:var(--ink2);}
  .spin{width:24px; height:24px; border:2px solid var(--line); border-top-color:var(--accent); border-radius:50%;
    margin:56px auto; animation:sp .7s linear infinite;} @keyframes sp{to{transform:rotate(360deg);}}
  ::-webkit-scrollbar{width:11px; height:11px;} ::-webkit-scrollbar-thumb{background:var(--line); border-radius:6px; border:3px solid var(--paper);}
</style>
</head>
<body>
<header>
  <span class="mark serif">T</span>
  <div class="wm">Database Time Machine<small>version history for your data</small></div>
  <div class="spacer"></div>
  <div class="dbpill" id="dbname">connected</div>
</header>
<div class="layout">
  <nav>
    <div class="nlabel">Views</div>
    <button data-view="overview" class="active">Overview</button>
    <button data-view="timeline">Timeline</button>
    <button data-view="timetravel">Time travel</button>
    <button data-view="diff">Compare</button>
    <button data-view="blame">Attribution</button>
    <button data-view="schema">Schema</button>
    <button data-view="tags">Checkpoints</button>
    <button data-view="branches">Branches</button>
    <button data-view="anomalies">Anomalies</button>
    <button data-view="integrity">Integrity</button>
  </nav>
  <main id="main"><div class="spin"></div></main>
</div>
<script>
const $=s=>document.querySelector(s);
async function api(p){const r=await fetch('/api/'+p);const d=await r.json();if(d&&d.error)throw new Error(d.error);return d;}
async function apiPost(p,body){const r=await fetch('/api/'+p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(d&&d.error)throw new Error(d.error);return d;}
function esc(v){return String(v==null?'':v).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cell(c,v){
  if(c==='op')return '<td><span class="tag '+esc(v)+'">'+esc(v)+'</span></td>';
  if(['change_id','pk','schema_id','ts','n'].includes(c))return '<td class="mono">'+esc(v)+'</td>';
  return '<td>'+esc(v)+'</td>';
}
function tbl(rows,cols){
  if(!rows||!rows.length)return '<div class="empty">Nothing to show yet.</div>';
  cols=cols||Object.keys(rows[0]);
  let h='<div class="tblwrap"><table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>';
  for(const r of rows)h+='<tr>'+cols.map(c=>cell(c,r[c])).join('')+'</tr>';
  return h+'</tbody></table></div>';
}
async function tableOptions(){const t=await api('tables');return t.map(x=>'<option>'+esc(x)+'</option>').join('');}
function bars(title,cap,arr,key){
  if(!arr.length)return '';
  const max=Math.max(...arr.map(a=>a.n))||1;
  let b='<div class="panel"><h3>'+title+'</h3><p class="cap">'+cap+'</p>';
  for(const a of arr)b+='<div class="barrow"><span class="k">'+esc(a[key])+'</span>'
    +'<span class="track"><span class="fill" data-w="'+(a.n/max*100).toFixed(0)+'"></span></span>'
    +'<span class="v">'+a.n+'</span></div>';
  return b+'</div>';
}
function animateBars(){requestAnimationFrame(()=>document.querySelectorAll('.fill').forEach(f=>f.style.width=f.dataset.w+'%'));}

const views={};

views.overview=async()=>{
  const s=await api('stats');
  let integ=''; try{const v=await api('verify');
    integ=v.ok?' · <b style="color:var(--accent)">✓ integrity verified</b>':' · <b style="color:var(--del)">✗ tampering detected</b>';}catch(e){}
  const st=(l,n,key)=>`<div class="st ${key?'key':''}"><div class="num">${n}</div><div class="lab">${l}</div></div>`;
  let h='<h1 class="vh">Overview</h1><p class="vsub">A complete record of everything that has happened to this database'+integ+'.</p>';
  h+='<div class="cards">'
    +st('Total changes',s.total_changes,true)+st('Commits',s.total_txns)
    +st('Tables',s.tables_tracked)+st('Checkpoints',s.tags)
    +st('Inserts',s.by_op.INSERT||0)+st('Updates',s.by_op.UPDATE||0)+st('Deletes',s.by_op.DELETE||0)+'</div>';
  h+='<div class="grid2">'+bars('By author','Who has made changes.',s.by_author,'author')
    +bars('By table','Where changes landed.',s.by_table,'tbl')+'</div>';
  h+=bars('Activity','Changes over time.',s.by_day,'day');
  h+='<div class="panel"><h3>Most-edited rows</h3><p class="cap">The rows that have changed most often.</p>'+tbl(s.most_edited_rows,['tbl','pk','n'])+'</div>';
  $('#main').innerHTML='<div class="fade">'+h+'</div>'; animateBars();
};

views.timeline=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Timeline</h1>
    <p class="vsub">Every recorded change, most recent first.</p>
    <div class="row">
      <div><label>Table</label><select id="t"><option value="">All tables</option>${opts}</select></div>
      <div><label>Author</label><input id="au" placeholder="anyone" style="width:130px"></div>
      <div><label>Operation</label><select id="op"><option value="">Any</option><option>INSERT</option><option>UPDATE</option><option>DELETE</option></select></div>
      <div><label>Search</label><input id="q" placeholder="message or value" style="width:180px"></div>
      <div><label>Limit</label><input id="lim" type="number" value="40" style="width:84px"></div>
      <button class="go" id="run">Search</button>
    </div><div id="out"><div class="spin"></div></div></div>`;
  const run=async()=>{const t=$('#t').value,lim=$('#lim').value,au=$('#au').value,op=$('#op').value,q=$('#q').value;
    let qs='log?limit='+lim;
    if(t)qs+='&table='+encodeURIComponent(t);
    if(au)qs+='&author='+encodeURIComponent(au);
    if(op)qs+='&op='+encodeURIComponent(op);
    if(q)qs+='&contains='+encodeURIComponent(q);
    const rows=await api(qs);
    if(!rows.length){$('#out').innerHTML='<div class="empty">Nothing to show yet.</div>';return;}
    let h='<div class="glog">';
    for(const r of rows){
      h+=`<div class="gnode ${esc(r.op)}"><div class="rail"><span class="node"></span></div>
        <div class="gcard"><div class="gtop"><span class="tag ${esc(r.op)}">${esc(r.op)}</span>
          <span class="gtbl">${esc(r.tbl)} · row ${esc(r.pk)}</span>
          <span class="ghash">#${esc(r.change_id)}</span>
          <span class="gts mono">${esc(r.ts)}</span></div>
          <div class="gmsg">${esc(r.message)||'<span class=hint>no message</span>'}</div>
          <div class="gauth">by <b>${esc(r.author)}</b></div></div></div>`;
    }
    $('#out').innerHTML=h+'</div>';};
  $('#run').onclick=run; run();
};

views.timetravel=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Time travel</h1>
    <p class="vsub">Reconstruct any table as it was &mdash; by timestamp, checkpoint name, or <code>now</code>.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>Point in time</label><input id="at" value="now" style="width:320px" placeholder="timestamp / checkpoint / now"></div>
      <button class="go" id="run">View state</button>
      <button class="go" id="rev" style="background:var(--del)">Restore this state</button>
    </div>
    <p class="hint" style="margin-bottom:22px">Restoring is recorded as a new change &mdash; you can always undo it.</p>
    <div id="out"></div></div>`;
  const run=async()=>{try{
    const rows=await api('asof?table='+encodeURIComponent($('#t').value)+'&at='+encodeURIComponent($('#at').value));
    $('#out').innerHTML=tbl(rows);
  }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
  $('#rev').onclick=async()=>{
    const t=$('#t').value,at=$('#at').value;
    if(!confirm('Restore "'+t+'" to the state at "'+at+'"?\n\nThis rewrites the live table (and is itself recorded).'))return;
    try{const r=await apiPost('revert',{table:t,at:at,author:'web-ui'});
      const s=r.summary;
      $('#out').innerHTML='<div class="panel" style="border-color:var(--accent)">Restored — inserted '+s.inserted+', updated '+s.updated+', deleted '+s.deleted+'.</div>';
      setTimeout(run,600);
    }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
  $('#run').onclick=run; run();
};

views.diff=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Compare</h1>
    <p class="vsub">What changed in a table between two moments.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>From</label><input id="f" placeholder="timestamp / checkpoint" style="width:240px"></div>
      <div><label>To</label><input id="to" value="now" style="width:240px"></div>
      <button class="go" id="run">Compare</button>
    </div><div id="out"></div></div>`;
  $('#run').onclick=async()=>{try{
    const d=await api('diff?table='+encodeURIComponent($('#t').value)+'&from='+encodeURIComponent($('#f').value)+'&to='+encodeURIComponent($('#to').value));
    const col=(cls,title,items,render)=>`<div class="dcol ${cls}"><h4>${title} <span class="cnt">${items.length}</span></h4>`
      +(items.length?items.map(render).join(''):'<div class="hint">None</div>')+'</div>';
    $('#out').innerHTML='<div class="diffbox">'
      +col('add','Added',d.added,x=>`<div class="dentry"><code>#${x.pk}</code> ${esc(JSON.stringify(x.row))}</div>`)
      +col('rem','Removed',d.removed,x=>`<div class="dentry"><code>#${x.pk}</code> ${esc(JSON.stringify(x.row))}</div>`)
      +col('chg','Changed',d.changed,x=>{
        const f=(x.fields||[]).map(k=>`<code>${esc(k)}</code>`).join(' ');
        return `<div class="dentry"><code>#${x.pk}</code> ${f?'changed '+f:''}<br><span class="rem-t">− ${esc(JSON.stringify(x.before))}</span><br><span class="add-t">+ ${esc(JSON.stringify(x.after))}</span></div>`;})
      +'</div>';
  }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
};

views.branches=async()=>{
  const rows=await api('branches');
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Branches</h1>
    <p class="vsub">Forks of this database you can merge back.</p>
    <p class="hint" style="margin-bottom:20px">Create one: <code>python -m dtm branch &lt;db&gt; &lt;name&gt;</code> · merge: <code>python -m dtm merge &lt;db&gt; &lt;name&gt;</code></p>
    <div class="panel">${tbl(rows,['name','created_from_ts','author','path'])}</div></div>`;
};

views.anomalies=async()=>{
  const rows=await api('anomalies?threshold=5');
  const note=rows.length?'Transactions that changed 5 or more rows at once — worth a second look.'
    :'No suspicious mass changes detected.';
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Anomalies</h1>
    <p class="vsub">${note}</p>
    <div class="panel">${tbl(rows,['txn_id','ts','op','tbl','rows_affected','author','message'])}</div></div>`;
};

views.integrity=async()=>{
  const r=await api('verify');
  const box=r.ok
    ? `<div class="panel" style="border-color:var(--accent)"><h3 style="color:var(--accent)">✓ Verified</h3>
        <p class="cap">The tamper-evident hash chain is intact across ${r.total} change(s). No records were altered or deleted after the fact.</p>
        <div class="hint mono" style="word-break:break-all">head: ${esc(r.head)}</div></div>`
    : `<div class="panel" style="border-color:var(--del)"><h3 style="color:var(--del)">✗ Tampering detected</h3>
        <p class="cap">The chain breaks at change #${esc(r.broken_at)} of ${esc(r.total)} — a past record was altered or removed.</p></div>`;
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Integrity</h1>
    <p class="vsub">Every change is chained by hash; any edit to history is detectable.</p>${box}</div>`;
};

views.blame=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Attribution</h1>
    <p class="vsub">Who last changed a column of a row, and why.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>Row (id)</label><input id="pk" type="number" value="1" style="width:110px"></div>
      <div><label>Column</label><select id="col"></select></div>
      <button class="go" id="run">Trace</button>
    </div><div id="out"></div><div id="hist"></div></div>`;
  const loadCols=async()=>{const cols=await api('columns?table='+encodeURIComponent($('#t').value));
    $('#col').innerHTML=cols.map(c=>'<option>'+esc(c.name)+'</option>').join('');};
  $('#t').onchange=loadCols; await loadCols();
  $('#run').onclick=async()=>{const t=$('#t').value,pk=$('#pk').value,col=$('#col').value;
    try{const b=await api('blame?table='+encodeURIComponent(t)+'&pk='+pk+'&column='+encodeURIComponent(col));
      if(!b){$('#out').innerHTML='<div class="empty">No change found for that row and column.</div>';$('#hist').innerHTML='';return;}
      const av=esc(String(b.author||'?').charAt(0).toUpperCase());
      $('#out').innerHTML=`<div class="panel"><div class="blamecard"><div class="avatar">${av}</div><div style="flex:1">
        <div style="font-size:16px; margin-bottom:2px"><b class="serif" style="font-size:17px">${esc(t)}.${esc(col)}</b> <span class="hint">of row ${esc(pk)}</span></div>
        <div class="hint" style="margin-bottom:15px">last changed by <b style="color:var(--ink)">${esc(b.author)}</b> · <span class="mono">${esc(b.ts)}</span></div>
        <div style="margin-bottom:13px"><span class="chip old">${esc(JSON.stringify(b.old_value))}</span><span class="arrow">→</span><span class="chip new">${esc(JSON.stringify(b.new_value))}</span></div>
        <div class="hint">“${esc(b.message)||'no message'}” &nbsp;·&nbsp; <span class="tag ${esc(b.op)}">${esc(b.op)}</span> &nbsp;·&nbsp; change #${esc(b.change_id)}</div>
      </div></div></div>`;
      const hist=await api('history?table='+encodeURIComponent(t)+'&pk='+pk);
      $('#hist').innerHTML='<div class="panel"><h3>Full history of this row</h3><p class="cap">Every change to row '+esc(pk)+'.</p>'+tbl(hist,['change_id','ts','op','author','message'])+'</div>';
    }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
};

views.schema=async()=>{
  const rows=await api('schema-log');
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Schema</h1>
    <p class="vsub">Every time the structure of the database changed.</p>
    <div class="panel">${tbl(rows,['schema_id','ts','author','message'])}</div></div>`;
};

views.tags=async()=>{
  const rows=await api('tags');
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Checkpoints</h1>
    <p class="vsub">Named points in time you can return to.</p>
    <p class="hint" style="margin-bottom:20px">Create one from the command line: <code>python -m dtm tag &lt;db&gt; &lt;name&gt;</code></p>
    <div class="panel">${tbl(rows,['name','ts','author','message'])}</div></div>`;
};

function activate(name){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
  $('#main').innerHTML='<div class="spin"></div>';
  views[name]().catch(e=>$('#main').innerHTML='<div class="err">'+esc(e.message)+'</div>');
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>activate(b.dataset.view));
api('tables').then(t=>{$('#dbname').innerHTML=(t.length||0)+' table'+(t.length===1?'':'s')+' tracked';}).catch(()=>{});
activate('overview');
</script>
</body>
</html>"""

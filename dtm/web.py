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
    if path == "/api/log":
        return tm.log(table=one("table") or None, limit=int(one("limit", "50")))
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
# Single-page app (HTML + CSS + vanilla JS, no external assets, works offline).
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Time Machine</title>
<style>
  :root{
    --bg0:#0a0b14; --bg1:#0e1020; --card:rgba(255,255,255,.035); --card-brd:rgba(255,255,255,.09);
    --card-hi:rgba(255,255,255,.06); --fg:#eef1fb; --muted:#8b93b4; --faint:#5c6485;
    --accent:#8b7bff; --accent2:#5b8cff; --grad:linear-gradient(135deg,#7c6cff,#4f9dff);
    --ins:#34d399; --ins-bg:rgba(52,211,153,.14); --upd:#fbbf24; --upd-bg:rgba(251,191,36,.14);
    --del:#fb7185; --del-bg:rgba(251,113,133,.14); --line:rgba(255,255,255,.07);
    --shadow:0 10px 40px -12px rgba(0,0,0,.6); --r:16px;
  }
  @media (prefers-color-scheme: light){
    :root:not([data-theme="dark"]){
      --bg0:#eef1f8; --bg1:#f7f9fd; --card:#ffffff; --card-brd:#e4e9f4; --card-hi:#f2f5fb;
      --fg:#141a2e; --muted:#5a6484; --faint:#8b93af; --line:#e7ebf4;
      --ins:#059669; --ins-bg:rgba(5,150,105,.10); --upd:#b45309; --upd-bg:rgba(180,83,9,.10);
      --del:#e11d48; --del-bg:rgba(225,29,72,.10);
      --shadow:0 10px 30px -14px rgba(20,30,70,.25);
    }
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{margin:0; color:var(--fg); font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:
      radial-gradient(900px 500px at 12% -8%, rgba(124,108,255,.16), transparent 60%),
      radial-gradient(800px 500px at 100% 0%, rgba(79,157,255,.12), transparent 55%),
      linear-gradient(180deg,var(--bg0),var(--bg1));
    background-attachment:fixed; -webkit-font-smoothing:antialiased;}
  .mono{font-family:ui-monospace,"SF Mono","JetBrains Mono",Consolas,monospace; font-variant-numeric:tabular-nums;}

  /* header */
  header{position:sticky; top:0; z-index:20; display:flex; align-items:center; gap:14px;
    padding:14px 22px; border-bottom:1px solid var(--line);
    background:rgba(10,11,20,.55); backdrop-filter:blur(14px);}
  @media (prefers-color-scheme: light){:root:not([data-theme="dark"]) header{background:rgba(255,255,255,.7);}}
  .brand{display:flex; align-items:center; gap:11px; font-weight:700; font-size:16px; letter-spacing:-.01em;}
  .logo{width:32px; height:32px; border-radius:10px; background:var(--grad); display:grid; place-items:center;
    box-shadow:0 6px 18px -6px rgba(124,108,255,.7); font-size:17px;}
  .brand small{display:block; font-weight:500; font-size:11px; color:var(--muted); letter-spacing:.02em;}
  .spacer{flex:1;}
  .dbpill{display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted);
    background:var(--card); border:1px solid var(--card-brd); padding:6px 12px; border-radius:999px;}
  .dot{width:8px; height:8px; border-radius:50%; background:var(--ins); box-shadow:0 0 0 0 rgba(52,211,153,.5);
    animation:pulse 2s infinite;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45);}70%{box-shadow:0 0 0 7px rgba(52,211,153,0);}100%{box-shadow:0 0 0 0 rgba(52,211,153,0);}}

  .layout{display:flex; min-height:calc(100vh - 61px);}
  nav{width:210px; flex:none; padding:18px 14px; border-right:1px solid var(--line);}
  .nlabel{font-size:10.5px; letter-spacing:.14em; color:var(--faint); text-transform:uppercase; padding:0 12px 10px;}
  nav button{display:flex; align-items:center; gap:11px; width:100%; text-align:left; cursor:pointer;
    background:none; border:none; color:var(--muted); padding:10px 12px; border-radius:11px; font-size:14px;
    font-weight:500; transition:.16s; margin-bottom:2px;}
  nav button svg{width:17px; height:17px; opacity:.8; flex:none;}
  nav button:hover{background:var(--card-hi); color:var(--fg);}
  nav button.active{background:var(--grad); color:#fff; box-shadow:0 8px 20px -10px rgba(124,108,255,.8);}
  nav button.active svg{opacity:1;}

  main{flex:1; padding:26px 30px; overflow:auto; min-width:0;}
  .vh{margin:0 0 4px; font-size:21px; font-weight:700; letter-spacing:-.02em;}
  .vsub{color:var(--muted); margin:0 0 22px; font-size:13.5px;}
  .fade{animation:fade .3s ease both;}
  @keyframes fade{from{opacity:0; transform:translateY(6px);}to{opacity:1; transform:none;}}

  /* stat cards */
  .cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr)); gap:14px; margin-bottom:22px;}
  .card{background:var(--card); border:1px solid var(--card-brd); border-radius:var(--r); padding:16px;
    box-shadow:var(--shadow); transition:.18s;}
  .stat{position:relative; overflow:hidden;}
  .stat:hover{transform:translateY(-3px); border-color:var(--accent);}
  .stat .ico{width:34px; height:34px; border-radius:10px; display:grid; place-items:center; margin-bottom:12px;
    background:linear-gradient(135deg,rgba(124,108,255,.22),rgba(79,157,255,.18)); color:var(--accent);}
  .stat .ico svg{width:18px; height:18px;}
  .stat .n{font-size:29px; font-weight:750; letter-spacing:-.02em; line-height:1;}
  .stat .l{color:var(--muted); font-size:11.5px; text-transform:uppercase; letter-spacing:.06em; margin-top:6px;}

  .panel{background:var(--card); border:1px solid var(--card-brd); border-radius:var(--r); padding:18px;
    box-shadow:var(--shadow); margin-bottom:18px;}
  .panel h3{margin:0 0 14px; font-size:13.5px; font-weight:650; letter-spacing:-.01em;}
  .grid2{display:grid; grid-template-columns:1fr 1fr; gap:18px;}
  @media(max-width:820px){.grid2{grid-template-columns:1fr;}}

  /* bars */
  .barrow{display:grid; grid-template-columns:120px 1fr 46px; align-items:center; gap:12px; margin:11px 0;}
  .barrow .k{color:var(--muted); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}
  .track{height:9px; border-radius:6px; background:var(--card-hi); overflow:hidden;}
  .fill{height:100%; border-radius:6px; background:var(--grad); width:0; transition:width .7s cubic-bezier(.22,1,.36,1);}
  .barrow .v{text-align:right; font-weight:650; font-size:13px;}

  /* tables */
  .tblwrap{border:1px solid var(--card-brd); border-radius:14px; overflow:hidden; background:var(--card);}
  table{border-collapse:collapse; width:100%; font-size:13px;}
  thead th{position:sticky; top:0; background:var(--card-hi); color:var(--muted); font-weight:600; text-align:left;
    padding:11px 14px; font-size:11px; letter-spacing:.05em; text-transform:uppercase; border-bottom:1px solid var(--line);}
  tbody td{padding:10px 14px; border-bottom:1px solid var(--line); white-space:nowrap;}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr{transition:.12s;}
  tbody tr:hover{background:var(--card-hi);}
  .badge{display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border-radius:999px; font-size:11px;
    font-weight:700; letter-spacing:.03em;}
  .badge::before{content:""; width:6px; height:6px; border-radius:50%; background:currentColor;}
  .badge.INSERT{color:var(--ins); background:var(--ins-bg);}
  .badge.UPDATE{color:var(--upd); background:var(--upd-bg);}
  .badge.DELETE{color:var(--del); background:var(--del-bg);}

  /* controls */
  .row{display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; margin-bottom:18px;}
  label{display:block; font-size:11.5px; color:var(--muted); margin-bottom:6px; font-weight:500;}
  select,input{background:var(--card); color:var(--fg); border:1px solid var(--card-brd); border-radius:10px;
    padding:9px 12px; font-size:14px; transition:.15s; outline:none;}
  select:focus,input:focus{border-color:var(--accent); box-shadow:0 0 0 3px rgba(124,108,255,.18);}
  button.go{background:var(--grad); color:#fff; border:none; border-radius:10px; padding:10px 18px; font-size:14px;
    font-weight:600; cursor:pointer; box-shadow:0 8px 20px -10px rgba(124,108,255,.8); transition:.15s;}
  button.go:hover{filter:brightness(1.08); transform:translateY(-1px);}
  button.ghost{background:var(--card); color:var(--fg); border:1px solid var(--card-brd); box-shadow:none;}

  /* diff */
  .diffbox{display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px;}
  @media(max-width:820px){.diffbox{grid-template-columns:1fr;}}
  .dcol{background:var(--card); border:1px solid var(--card-brd); border-radius:14px; padding:15px; box-shadow:var(--shadow);}
  .dcol h4{margin:0 0 12px; font-size:12.5px; display:flex; align-items:center; gap:8px;}
  .cnt{font-size:11px; padding:1px 8px; border-radius:999px; background:var(--card-hi); color:var(--muted); font-weight:700;}
  .dcol.add h4{color:var(--ins);} .dcol.rem h4{color:var(--del);} .dcol.chg h4{color:var(--upd);}
  .dentry{font-size:12px; padding:9px 11px; border-radius:9px; background:var(--card-hi); margin-bottom:8px; word-break:break-word;}
  .rem-t{color:var(--del);} .add-t{color:var(--ins);}

  /* blame */
  .blamecard{display:flex; gap:16px; align-items:flex-start;}
  .avatar{width:44px; height:44px; border-radius:12px; flex:none; display:grid; place-items:center; font-weight:800;
    font-size:17px; color:#fff; background:var(--grad); box-shadow:0 6px 16px -8px rgba(124,108,255,.8);}
  .chip{display:inline-block; padding:3px 10px; border-radius:8px; font-weight:600; font-size:13px;}
  .chip.old{color:var(--del); background:var(--del-bg);} .chip.new{color:var(--ins); background:var(--ins-bg);}
  .arrow{color:var(--faint); margin:0 8px;}

  .empty{color:var(--faint); padding:40px; text-align:center; font-size:13.5px;}
  .empty svg{width:30px; height:30px; opacity:.4; display:block; margin:0 auto 10px;}
  .err{color:var(--del); background:var(--del-bg); border:1px solid var(--del-bg); padding:12px 15px; border-radius:11px;}
  .spin{width:26px; height:26px; border:3px solid var(--card-hi); border-top-color:var(--accent); border-radius:50%;
    margin:44px auto; animation:sp .7s linear infinite;}
  @keyframes sp{to{transform:rotate(360deg);}}
  code{background:var(--card-hi); padding:2px 7px; border-radius:6px; font-size:12px; font-family:ui-monospace,Consolas,monospace;}
  .hint{color:var(--muted); font-size:12.5px;}
  ::-webkit-scrollbar{width:10px; height:10px;} ::-webkit-scrollbar-thumb{background:var(--card-brd); border-radius:6px;}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="logo">⏳</span>
    <span>Database Time Machine<small>git-style history &amp; time travel</small></span></div>
  <div class="spacer"></div>
  <div class="dbpill"><span class="dot"></span><span class="mono" id="dbname">connected</span></div>
</header>
<div class="layout">
  <nav>
    <div class="nlabel">Views</div>
    <button data-view="overview" class="active">__I_grid__ Overview</button>
    <button data-view="timeline">__I_list__ Timeline</button>
    <button data-view="timetravel">__I_clock__ Time Travel</button>
    <button data-view="diff">__I_diff__ Diff</button>
    <button data-view="blame">__I_user__ Blame</button>
    <button data-view="schema">__I_layers__ Schema</button>
    <button data-view="tags">__I_tag__ Tags</button>
  </nav>
  <main id="main"><div class="spin"></div></main>
</div>
<script>
const ICONS={
  grid:'<path d="M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z"/>',
  list:'<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  diff:'<path d="M12 3v18M5 8l-2 4 2 4M19 8l2 4-2 4"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
  layers:'<path d="M12 2l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5"/>',
  tag:'<path d="M3 3h8l10 10-8 8L3 11V3z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  db:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  commit:'<circle cx="12" cy="12" r="4"/><path d="M2 12h6M16 12h6"/>',
  edit:'<path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
  inbox:'<path d="M22 12h-6l-2 3h-4l-2-3H2M5 6l-3 6v6h20v-6l-3-6z"/>',
};
function icon(n,cls){return '<svg class="'+(cls||'')+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'+(ICONS[n]||'')+'</svg>';}
const $=s=>document.querySelector(s);
async function api(p){const r=await fetch('/api/'+p);const d=await r.json();if(d&&d.error)throw new Error(d.error);return d;}
function esc(v){return String(v==null?'':v).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cell(c,v){
  if(c==='op')return '<td><span class="badge '+esc(v)+'">'+esc(v)+'</span></td>';
  if(['change_id','pk','schema_id','ts','n'].includes(c))return '<td class="mono">'+esc(v)+'</td>';
  return '<td>'+esc(v)+'</td>';
}
function tbl(rows,cols){
  if(!rows||!rows.length)return '<div class="empty">'+icon('inbox')+'No rows to show</div>';
  cols=cols||Object.keys(rows[0]);
  let h='<div class="tblwrap"><table><thead><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr></thead><tbody>';
  for(const r of rows)h+='<tr>'+cols.map(c=>cell(c,r[c])).join('')+'</tr>';
  return h+'</tbody></table></div>';
}
async function tableOptions(){const t=await api('tables');return t.map(x=>'<option>'+esc(x)+'</option>').join('');}
function bars(title,arr,key){
  if(!arr.length)return '';
  const max=Math.max(...arr.map(a=>a.n))||1;
  let b='<div class="panel"><h3>'+title+'</h3>';
  for(const a of arr)b+='<div class="barrow"><span class="k">'+esc(a[key])+'</span>'
    +'<span class="track"><span class="fill" data-w="'+(a.n/max*100).toFixed(0)+'"></span></span>'
    +'<span class="v">'+a.n+'</span></div>';
  return b+'</div>';
}
function animateBars(){requestAnimationFrame(()=>document.querySelectorAll('.fill').forEach(f=>f.style.width=f.dataset.w+'%'));}

const views={};

views.overview=async()=>{
  const s=await api('stats');
  const c=(ico,l,n,color)=>`<div class="card stat"><div class="ico">${icon(ico)}</div>
    <div class="n">${n}</div><div class="l">${l}</div></div>`;
  let h='<h1 class="vh">Overview</h1><p class="vsub">Everything that has happened to this database at a glance.</p>';
  h+='<div class="cards">'
    +c('db','Changes',s.total_changes)+c('commit','Commits',s.total_txns)
    +c('layers','Tables',s.tables_tracked)+c('tag','Tags',s.tags)
    +c('inbox','Inserts',s.by_op.INSERT||0)+c('edit','Updates',s.by_op.UPDATE||0)
    +c('diff','Deletes',s.by_op.DELETE||0)+'</div>';
  h+='<div class="grid2">'+bars('Changes by author',s.by_author,'author')
    +bars('Changes by table',s.by_table,'tbl')+'</div>';
  h+=bars('Activity by day',s.by_day,'day');
  h+='<div class="panel"><h3>Most-edited rows</h3>'+tbl(s.most_edited_rows,['tbl','pk','n'])+'</div>';
  $('#main').innerHTML='<div class="fade">'+h+'</div>'; animateBars();
};

views.timeline=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Timeline</h1>
    <p class="vsub">Every recorded change, newest first.</p>
    <div class="row">
      <div><label>Table</label><select id="t"><option value="">All tables</option>${opts}</select></div>
      <div><label>Limit</label><input id="lim" type="number" value="50" style="width:100px"></div>
      <button class="go" id="run">Refresh</button>
    </div><div id="out"><div class="spin"></div></div></div>`;
  const run=async()=>{const t=$('#t').value,lim=$('#lim').value;
    const rows=await api('log?limit='+lim+(t?'&table='+encodeURIComponent(t):''));
    $('#out').innerHTML=tbl(rows,['change_id','ts','tbl','pk','op','author','message']);};
  $('#run').onclick=run; run();
};

views.timetravel=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Time Travel</h1>
    <p class="vsub">Reconstruct any table exactly as it was &mdash; by timestamp, tag, or <code>now</code>.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>Point in time</label><input id="at" value="now" style="width:340px" placeholder="timestamp / tag / now"></div>
      <button class="go" id="run">View state</button>
    </div>
    <p class="hint">To undo to this state: <code>python -m dtm revert &lt;table&gt; --to "&lt;point&gt;" -a you</code></p>
    <div id="out"></div></div>`;
  const run=async()=>{try{
    const rows=await api('asof?table='+encodeURIComponent($('#t').value)+'&at='+encodeURIComponent($('#at').value));
    $('#out').innerHTML=tbl(rows);
  }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
  $('#run').onclick=run; run();
};

views.diff=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Diff</h1>
    <p class="vsub">What changed in a table between two moments.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>From</label><input id="f" placeholder="timestamp / tag" style="width:250px"></div>
      <div><label>To</label><input id="to" value="now" style="width:250px"></div>
      <button class="go" id="run">Compare</button>
    </div><div id="out"></div></div>`;
  $('#run').onclick=async()=>{try{
    const d=await api('diff?table='+encodeURIComponent($('#t').value)+'&from='+encodeURIComponent($('#f').value)+'&to='+encodeURIComponent($('#to').value));
    const col=(cls,ico,title,items,render)=>`<div class="dcol ${cls}"><h4>${icon(ico)} ${title} <span class="cnt">${items.length}</span></h4>`
      +(items.length?items.map(render).join(''):'<div class="hint">none</div>')+'</div>';
    $('#out').innerHTML='<div class="diffbox">'
      +col('add','inbox','Added',d.added,x=>`<div class="dentry"><code>#${x.pk}</code> ${esc(JSON.stringify(x.row))}</div>`)
      +col('rem','diff','Removed',d.removed,x=>`<div class="dentry"><code>#${x.pk}</code> ${esc(JSON.stringify(x.row))}</div>`)
      +col('chg','edit','Changed',d.changed,x=>`<div class="dentry"><code>#${x.pk}</code><br><span class="rem-t">− ${esc(JSON.stringify(x.before))}</span><br><span class="add-t">+ ${esc(JSON.stringify(x.after))}</span></div>`)
      +'</div>';
  }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
};

views.blame=async()=>{
  const opts=await tableOptions();
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Blame</h1>
    <p class="vsub">Who last changed a column of a row, and why.</p>
    <div class="row">
      <div><label>Table</label><select id="t">${opts}</select></div>
      <div><label>Row (pk / rowid)</label><input id="pk" type="number" value="1" style="width:120px"></div>
      <div><label>Column</label><select id="col"></select></div>
      <button class="go" id="run">Blame</button>
    </div><div id="out"></div><div id="hist"></div></div>`;
  const loadCols=async()=>{const cols=await api('columns?table='+encodeURIComponent($('#t').value));
    $('#col').innerHTML=cols.map(c=>'<option>'+esc(c.name)+'</option>').join('');};
  $('#t').onchange=loadCols; await loadCols();
  $('#run').onclick=async()=>{const t=$('#t').value,pk=$('#pk').value,col=$('#col').value;
    try{const b=await api('blame?table='+encodeURIComponent(t)+'&pk='+pk+'&column='+encodeURIComponent(col));
      if(!b){$('#out').innerHTML='<div class="empty">'+icon('inbox')+'No change found for that row/column.</div>';$('#hist').innerHTML='';return;}
      const av=esc(String(b.author||'?').charAt(0).toUpperCase());
      $('#out').innerHTML=`<div class="panel"><div class="blamecard"><div class="avatar">${av}</div><div style="flex:1">
        <div style="font-size:15px; font-weight:650; margin-bottom:2px"><b>${esc(t)}.${esc(col)}</b> <span class="hint">of row ${esc(pk)}</span></div>
        <div class="hint" style="margin-bottom:12px">last set by <b style="color:var(--fg)">${esc(b.author)}</b> &middot; <span class="mono">${esc(b.ts)}</span></div>
        <div style="margin-bottom:10px"><span class="chip old">${esc(JSON.stringify(b.old_value))}</span><span class="arrow">→</span><span class="chip new">${esc(JSON.stringify(b.new_value))}</span></div>
        <div class="hint">💬 ${esc(b.message)||'<i>no message</i>'} &nbsp; <span class="badge ${esc(b.op)}">${esc(b.op)}</span> &nbsp; change #${esc(b.change_id)}</div>
      </div></div></div>`;
      const hist=await api('history?table='+encodeURIComponent(t)+'&pk='+pk);
      $('#hist').innerHTML='<div class="panel"><h3>Full row history</h3>'+tbl(hist,['change_id','ts','op','author','message'])+'</div>';
    }catch(e){$('#out').innerHTML='<div class="err">'+esc(e.message)+'</div>';}};
};

views.schema=async()=>{
  const rows=await api('schema-log');
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Schema history</h1>
    <p class="vsub">Every time the shape of the database changed.</p>
    <div class="panel">${tbl(rows,['schema_id','ts','author','message'])}</div></div>`;
};

views.tags=async()=>{
  const rows=await api('tags');
  $('#main').innerHTML=`<div class="fade"><h1 class="vh">Tags &amp; checkpoints</h1>
    <p class="vsub">Named points in time you can travel or revert to.</p>
    <p class="hint" style="margin-bottom:16px">Create one from the CLI: <code>python -m dtm tag &lt;db&gt; &lt;name&gt;</code></p>
    <div class="panel">${tbl(rows,['name','ts','author','message'])}</div></div>`;
};

// render nav icons
document.querySelectorAll('nav button').forEach(b=>{
  b.innerHTML=b.innerHTML.replace(/__I_(\w+)__/,(_,n)=>icon(n));
});
function activate(name){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
  $('#main').innerHTML='<div class="spin"></div>';
  views[name]().catch(e=>$('#main').innerHTML='<div class="err">'+esc(e.message)+'</div>');
}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>activate(b.dataset.view));
api('tables').then(t=>{$('#dbname').textContent=(t.length||0)+' table'+(t.length===1?'':'s');}).catch(()=>{});
activate('overview');
</script>
</body>
</html>"""

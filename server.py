#!/usr/bin/env python3
"""zebedee-gui — a live, read-only dashboard for a Zebedee records store.

Standard library only. htmx + server-sent events in the browser; this server renders HTML
fragments and pushes a `changed` event whenever anything under the records store changes.

Run from the project root (where z.toml lives), normally via `just z-agents-gui`:
    python zebedee/gui/server.py [--port 3333] [--host 127.0.0.1]

Never writes records. Agents write only through `just z-*`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))           # zebedee/ → import the helper's own loaders
import zcli  # noqa: E402

STATIC = {"htmx.min.js": "application/javascript", "htmx-sse.min.js": "application/javascript",
          "style.css": "text/css"}
TYPE_GLYPH = {"feature": "FEAT", "bug": "BUG", "enhancement": "ENH", "refactor": "RFCT", "chore": "CHORE"}

e = html.escape


# ---------------------------------------------------------------- data

class Store:
    """Thin read-only view over the records store, reusing zcli's loaders."""

    def __init__(self) -> None:
        self.ctx = zcli.Ctx()
        if not self.ctx.work.is_dir():
            sys.exit(f"zebedee-gui: no records store at {self.ctx.records} — run `just z-init` first")

    def items(self) -> list[dict]:
        return zcli.all_items(self.ctx)

    def runs(self) -> list[dict]:
        return zcli.load_runs(self.ctx)

    def agents(self) -> dict[str, dict]:
        out = {}
        for f in self.ctx.agents.glob("*.json"):
            try:
                a = json.loads(f.read_text())
                out[a["uuid"]] = a
            except (OSError, ValueError, KeyError):
                pass
        return out

    def events(self) -> list[dict]:
        evs = []
        for f in self.ctx.events.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                if line.strip():
                    try:
                        evs.append(json.loads(line))
                    except ValueError:
                        pass
        return sorted(evs, key=lambda x: x.get("ts", ""))

    def last_seen(self, aid: str) -> str | None:
        return zcli.last_activity(self.ctx, aid)

    def live(self, ts: str | None) -> bool:
        return not zcli.is_stale(ts, self.ctx.claim_timeout_h)

    def signature(self) -> str:
        """Cheap change detector: file count + newest mtime under the store (excluding its .git)."""
        n, newest = 0, 0
        for root, dirs, files in os.walk(self.ctx.records):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                if name.endswith((".tmp", ".duckdb")) or name in ("STATUS.md", "index.json"):
                    continue
                try:
                    newest = max(newest, os.stat(os.path.join(root, name)).st_mtime_ns)
                    n += 1
                except OSError:
                    pass
        return f"{n}-{newest}"


# ---------------------------------------------------------------- tiny helpers

def short(aid: str | None) -> str:
    return aid[-6:] if aid else "—"


def when(ts: str | None) -> str:
    """Compact local time: '15 Nov 14:02', or just the date for older items."""
    if not ts:
        return "—"
    try:
        d = dt.datetime.fromisoformat(ts).astimezone()
    except ValueError:
        return e(ts)
    now = dt.datetime.now().astimezone()
    return d.strftime("%d %b %H:%M") if (now - d).days < 180 else d.strftime("%d %b %Y")


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        s = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return "?"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def md(text: str) -> str:
    """Minimal markdown: headings, bullets, paragraphs, `code`, **bold**, _em_. Enough for item docs."""
    def inline(s: str) -> str:
        s = e(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"<em>\1</em>", s)
        return s
    out, para, in_list, in_code = [], [], False, False
    def flush():
        nonlocal para
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(e(raw)); continue
        if not line.strip():
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            continue
        if line.lstrip().startswith("#"):
            flush()
            out.append(f"<h5>{inline(line.lstrip('# '))}</h5>"); continue
        m = re.match(r"\s*(?:[-*]|\d+\.)\s+(.*)", line)
        if m:
            flush()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(m.group(1))}</li>"); continue
        para.append(line.strip())
    flush()
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def trace(values: list[float], w: int = 300, h: int = 70, pad: int = 6) -> str:
    """An oscilloscope-style SVG polyline."""
    if not values:
        values = [0, 0]
    if len(values) == 1:
        values = values * 2
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = (w - 2 * pad) / (len(values) - 1)
    pts = " ".join(f"{pad + i * step:.1f},{h - pad - (v - lo) / span * (h - 2 * pad):.1f}"
                   for i, v in enumerate(values))
    return (f'<svg class="trace" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{pts}"/></svg>')


def first_number(metrics: dict) -> tuple[str, float] | None:
    for k, v in metrics.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return k, float(v)
    return None


def fmt_num(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 100 or v == int(v) else f"{v:.3g}"


# ---------------------------------------------------------------- fragments

def frag_board(store: Store) -> str:
    items = store.items()
    agents = store.agents()
    cols = []
    for s, name in zcli.STAGES.items():
        here = [i for i in items if i["stage"] == s]
        here.sort(key=lambda i: (i["status"] != "active", i.get("created", "")))
        cards = []
        for it in here:
            owner = it.get("owner")
            dot = ""
            if owner:
                live = store.live(store.last_seen(owner) or it.get("claimed_at"))
                who = agents.get(owner, {})
                tip = f"claimed by {who.get('harness', '?')} | {who.get('model', '?')} · {short(owner)}"
                dot = f'<span class="claim {"live" if live else "stale"}" title="{e(tip)}"></span>'
            closed = it["status"] != "active"
            flags = []
            if it.get("migrated"):
                flags.append('<span class="flag" title="migrated history">HIST</span>')
            if closed:
                flags.append(f'<span class="flag st-{e(it["status"])}">{e(it["status"].upper())}</span>')
            cards.append(f"""
<button class="card t-{e(it['type'])}{' closed' if closed else ''}" hx-get="/f/item/{e(it['id'])}"
        hx-target="#drawer" hx-swap="innerHTML">
  <span class="card-top"><span class="type">{TYPE_GLYPH.get(it['type'], e(it['type'][:5].upper()))}</span>{dot}</span>
  <span class="title">{e(it['title'])}</span>
  <span class="meta">{e(", ".join(it.get('domains', [])))}</span>
  <span class="meta">{when(it.get('created'))} {''.join(flags)}</span>
</button>""")
        live_n = sum(1 for i in here if i["status"] == "active")
        zone = "explore" if s < 0 else ("gate" if s == 0 else "commit")
        label = f"{s:+d}" if s else "0"
        cols.append(f"""
<div class="col {zone}">
  <div class="col-head"><span class="col-n">{label}</span><span class="col-name">{e(name)}</span>
    <span class="col-count">{live_n or ""}</span></div>
  <div class="col-body">{''.join(cards) or '<span class="empty">·</span>'}</div>
</div>""")
    return "".join(cols)


def frag_panel(store: Store) -> str:
    items, runs, agents, events = store.items(), store.runs(), store.agents(), store.events()
    active = [i for i in items if i["status"] == "active"]
    spikes = [i for i in active if i["stage"] == -1]
    live_agents = [a for a in agents.values() if store.live(store.last_seen(a["uuid"]))]
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("done", "parked", "rejected")}

    # activity: real write activity (recorded_at) per hour over the last 48h
    now = dt.datetime.now().astimezone()
    buckets = [0] * 48
    for ev in events:
        try:
            t = dt.datetime.fromisoformat(ev.get("recorded_at") or ev["ts"])
        except (KeyError, ValueError):
            continue
        h = int((now - t).total_seconds() // 3600)
        if 0 <= h < 48:
            buckets[47 - h] += 1

    # benchmarks: first numeric metric per bench name, last 24 live runs
    bench: dict[str, list[tuple[str, float]]] = {}
    for r in runs:
        if r.get("kind") == "bench":
            fn = first_number(r.get("metrics", {}))
            if fn:
                bench.setdefault(r["name"], []).append(fn)
    bench_rows = []
    for name, vals in sorted(bench.items(), key=lambda kv: kv[0])[:6]:
        key, latest = vals[-1]
        bench_rows.append(f"""
<div class="bench"><div class="bench-head"><span>{e(name)}</span><span class="bench-val">{fmt_num(latest)}
  <small>{e(key)}</small></span></div>{trace([v for _, v in vals[-24:]], 300, 34, 3)}</div>""")

    agent_rows = []
    for a in sorted(agents.values(), key=lambda a: store.last_seen(a["uuid"]) or "", reverse=True)[:8]:
        seen = store.last_seen(a["uuid"])
        holds = [i["id"] for i in active if i.get("owner") == a["uuid"]]
        agent_rows.append(f"""
<li class="{'live' if store.live(seen) else 'stale'}"><span class="claim {'live' if store.live(seen) else 'stale'}"></span>
  <span class="who">{e(a.get('harness', '?'))} <em>{e(a.get('model', '?'))}</em></span>
  <span class="seen">{ago(seen)}{f' · holds {len(holds)}' if holds else ''}</span></li>""")

    parked = [i for i in items if i["status"] == "parked"][:5]
    parked_rows = "".join(
        f'<li hx-get="/f/item/{e(i["id"])}" hx-target="#drawer"><span>{e(i["title"])}</span>'
        f'<em>revisit: {e(str(i.get("revisit") or "—"))}</em></li>' for i in parked)

    return f"""
<div class="readouts">
  <div class="readout"><b>{len(active)}</b><span>Active</span></div>
  <div class="readout"><b>{len(spikes)}</b><span>Spikes</span></div>
  <div class="readout"><b>{len(live_agents)}</b><span>Agents live</span></div>
</div>
<div class="tally">DONE {counts['done']} · PARKED {counts['parked']} · REJECTED {counts['rejected']} · RUNS {len(runs)}</div>

<div class="scope"><span class="scope-label">Activity · 48 h</span>{trace(buckets)}</div>

<h3>Benchmarks</h3>
{''.join(bench_rows) or '<p class="quiet">No benchmark runs yet.</p>'}

<h3>Agents</h3>
<ul class="agents">{''.join(agent_rows) or '<li class="quiet">No agents registered.</li>'}</ul>

{f'<h3>Parked</h3><ul class="parked">{parked_rows}</ul>' if parked_rows else ''}

<p class="note"><em>Zebedee, 2026.</em> An agent-native work ledger. Ideas move left to right across the
plate; at the gate each spike is committed, parked or rejected, and every decision and benchmark
stays on the record.</p>"""


def frag_item(store: Store, iid: str) -> str:
    try:
        d, meta, body = zcli.load_item(store.ctx, iid)
    except SystemExit:
        return '<div class="drawer-inner"><p class="quiet">Item not found.</p></div>'
    agents = store.agents()
    owner = meta.get("owner")
    who = agents.get(owner, {}) if owner else {}
    docs = []
    for kind in ("decision", "spike", "spec"):
        f = d / f"{kind}.md"
        if f.exists():
            docs.append(f'<details{" open" if kind == "decision" else ""}><summary>{kind}.md</summary>'
                        f'<div class="doc">{md(f.read_text())}</div></details>')
    runs = [r for r in store.runs() if r.get("item") == meta["id"]][-10:]
    run_rows = "".join(
        f'<tr><td>{when(r["ts"])}</td><td>{e(r["kind"])}</td><td>{e(r["name"])}</td>'
        f'<td>{ {True: "pass", False: "FAIL", None: "–"}[r.get("passed")] }</td>'
        f'<td class="mono">{e(json.dumps(r.get("metrics", {}))[:60])}</td></tr>' for r in runs)
    evs = [ev for ev in store.events() if ev.get("item") == meta["id"]]
    ev_rows = "".join(
        f'<li><span class="mono">{when(ev.get("ts"))}</span> {e(ev.get("action", ""))}'
        f'{" " + e(str(ev.get("frm"))) + "→" + e(str(ev.get("to"))) if "to" in ev else ""}'
        f'{" · " + e(ev["verdict"]) if ev.get("verdict") else ""}'
        f'{" · " + e(ev["reason"]) if ev.get("reason") else ""}'
        f'<span class="by">{short(ev.get("agent"))}</span></li>' for ev in evs)
    fields = [
        ("Domains", ", ".join(meta.get("domains", []))),
        ("Owner", f"{who.get('harness', '?')} | {who.get('model', '?')} · {short(owner)}" if owner else "unclaimed"),
        ("Created", when(meta.get("created")) + (" (migrated)" if meta.get("migrated") else "")),
        ("Entered at", f"{meta.get('entry_stage')}" + (f" — {meta['entry_note']}" if meta.get("entry_note") else "")),
        ("Revisit", meta.get("revisit") or ""),
        ("Commits", ", ".join(meta.get("commits", []))),
        ("Sources", ", ".join(meta.get("sources", []))),
    ]
    field_rows = "".join(f"<dt>{k}</dt><dd>{e(str(v))}</dd>" for k, v in fields if v not in ("", None))
    stage = meta["stage"]
    return f"""
<div class="drawer-inner" hx-get="/f/item/{e(meta['id'])}" hx-trigger="sse:changed" hx-swap="outerHTML">
  <div class="screw tl"></div><div class="screw tr"></div><div class="screw bl"></div><div class="screw br"></div>
  <button class="close" onclick="document.getElementById('drawer').innerHTML=''" title="Close">×</button>
  <div class="drawer-id">{e(meta['id'])}</div>
  <h2>{e(meta['title'])}</h2>
  <div class="readouts">
    <div class="readout"><b>{stage:+d}</b><span>{e(zcli.STAGES.get(stage, '?'))}</span></div>
    <div class="readout"><b class="word">{TYPE_GLYPH.get(meta['type'], e(meta['type'][:5].upper()))}</b><span>Type</span></div>
    <div class="readout"><b class="word">{e(meta['status'].upper())}</b><span>Status</span></div>
  </div>
  <dl>{field_rows}</dl>
  <div class="doc">{md(body)}</div>
  {''.join(docs)}
  {f'<h3>Runs</h3><table class="runs">{run_rows}</table>' if run_rows else ''}
  {f'<h3>History</h3><ul class="history">{ev_rows}</ul>' if ev_rows else ''}
</div>"""


def page(store: Store) -> str:
    name = store.ctx.root.name
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zebedee · {e(name)}</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/htmx.min.js"></script>
<script src="/static/htmx-sse.min.js"></script>
</head>
<body hx-ext="sse" sse-connect="/events">
<header>
  <h1>Zebedee <em>Ledger</em></h1>
  <div class="masthead">PROJECT · {e(name.upper())} · <span id="live" class="off">● OFFLINE</span>
    <button class="toggle" onclick="document.body.classList.toggle('show-closed');this.classList.toggle('on')">Closed</button></div>
</header>
<main>
  <section class="plate">
    <div class="screw tl"></div><div class="screw tr"></div><div class="screw bl"></div><div class="screw br"></div>
    <div class="zones"><span class="z-explore">Explore</span><span class="z-gate">Gate</span><span class="z-commit">Committed</span></div>
    <div id="board" class="board" hx-get="/f/board" hx-trigger="load, sse:changed" hx-swap="innerHTML"></div>
  </section>
  <aside class="panel" hx-get="/f/panel" hx-trigger="load, sse:changed" hx-swap="innerHTML"></aside>
</main>
<div id="drawer"></div>
<script>
  const live = document.getElementById('live');
  document.body.addEventListener('htmx:sseOpen', () => {{ live.className = 'on'; live.textContent = '● LIVE'; }});
  document.body.addEventListener('htmx:sseError', () => {{ live.className = 'off'; live.textContent = '● OFFLINE'; }});
  document.addEventListener('keydown', ev => {{ if (ev.key === 'Escape') document.getElementById('drawer').innerHTML = ''; }});
</script>
</body></html>"""


# ---------------------------------------------------------------- server

class Watcher(threading.Thread):
    """One thread polls the store once a second; every SSE client reads the shared signature."""

    def __init__(self, store: Store, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.store, self.interval = store, interval
        self.sig = store.signature()

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            try:
                self.sig = self.store.signature()
            except OSError:
                pass


def make_handler(store: Store, watcher: Watcher):
    class Handler(BaseHTTPRequestHandler):
        server_version = "zebedee-gui"

        def log_message(self, fmt, *args):  # quiet: no per-request logging
            pass

        def send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            path = unquote(url.path)
            try:
                if path == "/":
                    return self.send(200, page(store))
                if path == "/f/board":
                    return self.send(200, frag_board(store))
                if path == "/f/panel":
                    return self.send(200, frag_panel(store))
                if path.startswith("/f/item/"):
                    iid = path[len("/f/item/"):]
                    if not re.fullmatch(r"[A-Za-z0-9._-]+", iid):
                        return self.send(400, "bad id", "text/plain")
                    return self.send(200, frag_item(store, iid))
                if path.startswith("/static/"):
                    name = path[len("/static/"):]
                    if name in STATIC:
                        return self.send(200, (HERE / "static" / name).read_bytes(), STATIC[name])
                if path == "/events":
                    return self.stream()
                self.send(404, "not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass

        def stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last, beat = watcher.sig, time.monotonic()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while True:
                    time.sleep(0.5)
                    if watcher.sig != last:
                        last = watcher.sig
                        self.wfile.write(f"event: changed\ndata: {last}\n\n".encode())
                        self.wfile.flush()
                        beat = time.monotonic()
                    elif time.monotonic() - beat > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        beat = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(prog="zebedee-gui", description="Live read-only dashboard for Zebedee")
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (local only)")
    a = ap.parse_args()
    store = Store()
    watcher = Watcher(store)
    watcher.start()
    try:
        srv = ThreadingHTTPServer((a.host, a.port), make_handler(store, watcher))
    except OSError as err:
        sys.exit(f"zebedee-gui: can't listen on {a.host}:{a.port} ({err.strerror}) — is it already running?")
    srv.daemon_threads = True
    print(f"zebedee-gui · {store.ctx.root.name} · http://{a.host}:{a.port}  (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()

# zebedee-gui

A live, read-only dashboard for a [Zebedee](https://github.com/ianscrivener/zebedee) records store.

- **Board:** active items across the stages, from Explore through the Gate to Committed. Claimed items show a teal dot (live session) or a rust dot (stale claim).
- **Panel:** readouts, 48-hour activity trace, benchmark traces, agent sessions, parked items.
- **Item drawer:** click a card for its fields, intent, decision/spike/spec docs, runs and history.

It updates itself: the server watches `.records/` and pushes a server-sent event whenever anything changes.

## Stack

Python standard library (`http.server`) + [htmx](https://htmx.org) 2 with its SSE extension, vendored in `static/` (works offline). No build step and nothing to install. It imports the Zebedee helper (`zebedee/zcli.py`) to read records, so there's one loader, not two.

**Read-only.** It never writes records; agents write only through `just z-*`.

## Use

It lives in a Zebedee project as a git submodule at `zebedee/gui`:

```
git submodule update --init zebedee/gui     # once, if the folder is empty
just z-agents-gui                            # http://127.0.0.1:3333
just z-agents-gui --port 4000                # another port
```

It listens on `127.0.0.1` only. Records can be private, so `--host 0.0.0.0` is opt-in.

## Licences

MIT (this repo). htmx and htmx-ext-sse are BSD Zero-Clause (`static/`).

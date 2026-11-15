# Install prompt: add zebedee-gui to a Zebedee project

> **Usage:** open your agent at the root of a project that already uses Zebedee and say:
>
> *"Follow https://github.com/ianscrivener/zebedee-gui/blob/main/INSTALL_PROMPT.md"*
>
> Defaults: the project is the current working directory, the port is 3333, and nothing is committed or pushed.

---

You are adding **zebedee-gui**, a live, read-only dashboard for Zebedee records, to the project in your current working directory. It's added as a **git submodule** at `zebedee/gui` and started with `just z-agents-gui`.

This is a small, mechanical job: no more than a few minutes. Don't explore beyond what each step needs.

## Ground rules

1. **Don't commit or push.** Leave the changes staged for the human. `git submodule add` stages `.gitmodules` and the submodule itself; stage any other file you change too.
2. **Don't overwrite** existing files or recipes. If something already exists, check it and adapt; don't replace it.
3. **The dashboard is read-only and local.** Never change it to write records, and never make it listen beyond `127.0.0.1` unless the human asks.

## Step 1: Check preconditions

Run these checks and **stop with a clear message** if one fails:

| Check | How | If it fails |
|---|---|---|
| It's a git repo | `git rev-parse --show-toplevel` equals the current directory | Stop. Ask the human to run this from the project's root |
| It uses Zebedee | `z.toml`, `zebedee/zcli.py` and `zebedee/zebedee.just` all exist | Stop. Point the human to the retrofit prompt: https://github.com/ianscrivener/zebedee/blob/main/RETROFIT_PROMPT.md |
| The records store exists | `.records/work/` exists (or `$Z_RECORDS` points to one) | Tell the human to run `just z-init` first, then stop |
| Python ≥3.10 is available | The command in `.zebedee-cmd` or `$ZEBEDEE_CMD` if present, otherwise `uv run python --version` or `python3 --version` | Stop and say which Python was found |
| Port free | `lsof -i :3333` (macOS/Linux) shows nothing listening | Not fatal: note it, and use `--port` in Step 4 |

## Step 2: Add the submodule

- If `zebedee/gui/server.py` **already exists**, it's installed. Run `git submodule update --init zebedee/gui`, then skip to Step 3.
- If `zebedee/gui` exists but is **empty**, it's registered but not checked out. Run `git submodule update --init zebedee/gui`.
- Otherwise:
  ```
  git submodule add https://github.com/ianscrivener/zebedee-gui zebedee/gui
  ```

Then confirm `zebedee/gui/server.py` and `zebedee/gui/static/htmx.min.js` exist.

## Step 3: Make sure the recipe exists

Look for a `z-agents-gui` recipe: `just --list | grep z-agents-gui`.

- **Present** (Zebedee 0.2+ includes it): nothing to do.
- **Missing:** append this recipe to `zebedee/zebedee.just`, after the `z-agents-sessions` recipe if there is one. Copy it exactly, indentation included. If the file has no `zebedee_cmd` variable, replace `cmd="{{zebedee_cmd}}"` with `cmd="python3 zebedee/zcli.py"`.

  ```just
  # Live read-only dashboard on http://127.0.0.1:3333 (zebedee-gui submodule). Args: --port N, --host H
  [group('human: agents')]
  [positional-arguments]
  z-agents-gui *args:
      #!/usr/bin/env bash
      set -euo pipefail
      [ -f zebedee/gui/server.py ] || { echo "zebedee-gui not present — run: git submodule update --init zebedee/gui" >&2; exit 1; }
      cmd="{{zebedee_cmd}}"
      exec ${cmd/zebedee\/zcli.py/zebedee\/gui\/server.py} "$@"
  ```

  Check it parses: `just --list` must run without errors and show `z-agents-gui`.

## Step 4: Smoke-test it

Start it briefly on a spare port, check the main endpoints, and let `timeout` stop it:

```
timeout 8 just z-agents-gui --port 3399 >/tmp/zebedee-gui.log 2>&1 &
sleep 3
curl -s -o /dev/null -w "page %{http_code}\n"  http://127.0.0.1:3399/
curl -s -o /dev/null -w "board %{http_code}\n" http://127.0.0.1:3399/f/board
curl -s -o /dev/null -w "panel %{http_code}\n" http://127.0.0.1:3399/f/panel
```

All three must return `200`. If not, read `/tmp/zebedee-gui.log` and fix the cause (usually Python or the recipe), within these rules. Don't edit `server.py`.

## Step 5: Tidy up

- Make sure `__pycache__/` is ignored by the project's `.gitignore`. Add it if it isn't.
- If the project's README has a section on Zebedee or developer tools, add one line: *"Dashboard: `just z-agents-gui` → http://127.0.0.1:3333 (zebedee-gui submodule)."* Otherwise leave the README alone.
- `git status --short`: everything you changed should be staged.

## Step 6: Report to the human

Finish with a short message:

- **Installed:** submodule at `zebedee/gui` (commit `git -C zebedee/gui rev-parse --short HEAD`), and whether you added the recipe.
- **Start it:** `just z-agents-gui`, then open http://127.0.0.1:3333.
- **Teammates and other worktrees** need `git submodule update --init zebedee/gui` once. New clones can use `git clone --recurse-submodules`.
- **Containers:** run the dashboard on the host, not in an agent container. It only reads `.records/`.
- **Left staged, not committed:** the list of files.

## Feedback

If any step above was wrong, unclear or failed, open a short issue on `ianscrivener/zebedee-gui`:

```
gh issue create --repo ianscrivener/zebedee-gui --title "Install feedback: <project> (<date>)" --body-file <file> --label install-feedback
```

Say what happened, at which step, and what would have helped. If the label doesn't exist, file without it; if `gh` isn't available, give the text to the human. Leave out secrets and private paths. If everything went smoothly, skip this.

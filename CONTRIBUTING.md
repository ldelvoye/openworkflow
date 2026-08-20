# Contributing

## Setup

```
uv sync --all-extras --dev
```

Four gates, all green before any PR:

```
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

## What the barebones integration looks like

An integration is one directory and one registry line. Read
[docs/architecture.md](docs/architecture.md) first — it explains *why* the
boundaries below exist.

```
src/smorg/integrations/<id>/
  manifest.py   what your integration is
  source.py     how its data is fetched
  panel.py      how its tab looks
```

Then add your `INTEGRATION` to `INTEGRATIONS` in
`src/smorg/integrations/__init__.py`. That allowlist is deliberate: anything
not registered fails with "not supported" rather than half-working.

### What you own

- **`manifest.py`** — id, display name, declared connection paths (each a
  `ConnectionPath` naming either a `ProviderConfig` to run OAuth against or a
  `TokenPrompt` asking for a token the user pastes in), `stale_after`, declared
  actions (each tagged `local` / `launch`; `remote` is not implemented), and
  your panel class.
- **`source.py`** — `fetch(credentials, http)` returning your `Item` subclass,
  and `fetch_detail` if your panel shows details. Pagination, filtering, and
  any service quirks are yours on purpose: measured across real MCP servers,
  none of it generalizes (see architecture.md). Sanitize server text with
  `sanitize_block`, then service-specific normalization, then `truncate` — in
  that order.
- **`panel.py`** — extend `Panel`, override its render hooks, and decide your
  policy: grouping, glyphs, ordering, and _when an interaction marks an item
  seen_ (an opt-in capability — see "What the core provides"). `render_ready()`
  returns any renderable, so a tab is not obliged to be one list — GitHub's is
  two columns. Override `ready_text()` beside it whenever `render_ready()` is
  not a `Text`; that is what the stale banner sits above and what your tests
  read.

## What the core provides

**Runs for you, no code on your side:**

- Credential storage and, for an OAuth path, the browser login and refresh —
  all through your declared connection paths. Declaring a `TokenPrompt`
  instead gets you the masked in-app field, the `getpass` prompt on the CLI,
  and the same storage; there is nothing to refresh, so a token that stops
  working reaches you as `AuthExpired` and the tab says to connect again.
  `fetch` receives credentials per call; you never touch or persist a token
  yourself.
- Refresh scheduling that follows attention, not a clock.
- Per-tab failure isolation driven by the `IntegrationError` taxonomy
  (`AuthExpired` / `AccessNotAllowed` / `Unavailable` / `Malformed`).
- Seen-state loading and injection, so `mark_seen` below always has a live
  store to write into.

**Building blocks you call:**

- `McpSession` (`core/mcp.py`) — MCP calls with handshake caching and
  retry-once.
- `required_string` / `optional_string` / `timestamp` (`core/shape.py`) —
  untrusted-shape guards that raise `Malformed`.
- `sanitize_line` / `sanitize_block` / `truncate` (`core/text.py`) — sanitizing server text.
- `Panel` and its render hooks (`shell/panel.py`) — the five states, the
  detail region, `mark_seen()` / `mark_all_seen()`.
- the theme-safe `Markdown` widget (`shell/markdown.py`) — clickable links,
  local-path underlining.
- `age()` (`shell/format.py`).

**Optional capabilities you opt into:**

- Change marks — call `self.mark_seen(item)` when an interaction should
  count as "seen"; skip it and the feature simply doesn't exist for your tab.
- The detail pane — implement `fetch_detail` (its own `SupportsDetail` protocol, feature-detected
  by the shell); the shell fetches and caches it for you off the UI thread, so your panel never
  touches the network. An integration without a detail pane simply doesn't define it.
- Declared `Action`s — validated against reserved and duplicate keys at
  construction and surfaced in the `?` help listing; the key itself is still
  yours to bind, in `panel.py`'s own `BINDINGS`.

## What development support you have

**Screenshots.** A reviewer won't have every integration's service account —
nobody expects them to sign up for Azure just to review a contributed Azure
tab. Press `^ + p` and run **Screenshot** to export the current screen as an
SVG to your Downloads folder, rendered with your terminal's real colors
instead of a generic fallback, and lifted to a 4.5:1 readability floor so no
text exports fainter than it draws (`export_screenshot` is overridden in
`shell/app.py` to use the palette this app learns from the terminal at
startup — see `shell/terminal_palette.py`). Attach one to your PR for any UI
change; it's how a reviewer judges the look of a tab they can't connect
themselves.

**Sandboxed local runs.** Point `SMORG_CONFIG_DIR` at a scratch directory and
set `SMORG_CREDENTIAL_STORE=file` to run against disk instead of the OS
keychain — the same seams the test suite uses. Tests themselves run with no
network access: sources are tested against recorded payloads, panels against
constructed items.

## What is expected: code quality, comment quality, test quality

- Name intermediates: no resolving and destructuring a value in the same
  expression. More lines beat a dense one-liner.
- No `x or y` or a ternary fused into a call as a value-selection argument —
  assign the chosen value to a named variable with an explicit if/else first.
- Comments and docstrings state the end result, ideally with an input→output
  example. If a comment is needed to explain the mechanism, clarify the code
  instead, then trim the comment. A constraint the code can't show (call
  ordering, a protocol quirk) earns one line.
- Commits: `type(scope): summary`, lowercase, imperative. Bodies only when the
  diff can't say it.

A test here asserts a decision of this codebase — a contract, an enforced
seam, a security property, a policy — never a library's own behavior, and
never coverage for its own sake. New work adds tests only where it adds
decisions; a diff that grows tests without new decisions should cut them
instead. Two shapes look thin but earn their place: a positive-case control
that keeps a rejection test honest, and the same security property
re-asserted at each call site, since call sites regress independently.

### Rules the test suite enforces

- **Sources never format; panels never fetch.** The Linear panel carries a
  grep-based test enforcing this (`test_the_panel_never_fetches`); copy it for
  your integration.
- **Errors cross the seam only as `IntegrationError`** — pick `AuthExpired`
  / `AccessNotAllowed` / `Unavailable` / `Malformed` by what would fix the
  failure; the semantics live in [docs/architecture.md](docs/architecture.md).
- **Response shape is untrusted.** A server field that should be an object may
  be a string; that must surface as `Malformed`, never a traceback.
- **Reserved keys can't be bound.** The shell owns its keymap
  (`core/keys.py: RESERVED_KEYS`); a manifest binding one is rejected at
  construction.
- **No tokens in output.** Error messages carry no credential material —
  including a token a user typed and got wrong; server-controlled text is
  sanitized before it can reach a terminal.

## Releasing

`pyproject.toml`'s `version` is the only place a release is recorded —
`__version__` and everything else read it from installed package metadata.

1. `uv version --bump <patch|minor|major>`, which bumps `version` in
   `pyproject.toml` and syncs the lockfile in one step.
2. Prune the released section from [docs/ROADMAP.md](docs/ROADMAP.md).
3. Four gates green (see Setup above).
4. Commit `chore: release vX.Y.Z`, tag `vX.Y.Z` on that commit, then push the
   branch and the tag.
5. `gh release create vX.Y.Z --latest --notes "..."` — a bare tag never shows
   under GitHub's Releases, only a release does.

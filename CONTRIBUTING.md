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

Tests run with no network access — sources are tested against recorded
payloads, panels against constructed items.

## Adding an integration

An integration is one directory and one registry line. Read
[docs/architecture.md](docs/architecture.md) first — it explains _why_ the
boundaries below exist.

```
src/oflow/integrations/<id>/
  manifest.py   what your integration is
  source.py     how its data is fetched
  panel.py      how its tab looks
```

Then add your `INTEGRATION` to `INTEGRATIONS` in
`src/oflow/integrations/__init__.py`. That allowlist is deliberate: anything
not registered fails with "not supported" rather than half-working.

### What you inherit (don't rebuild these)

| From                  | You get                                                                                                                                       |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `core/mcp.py`         | `McpSession` — MCP calls with handshake caching and retry-once                                                                                |
| `core/shape.py`       | `required_string` / `optional_string` / `timestamp` — untrusted-shape guards that raise `Malformed`                                           |
| `core/text.py`        | `printable` / `printable_block` / `capped` — sanitizing server text                                                                           |
| `shell/panel.py`      | `Panel` — the five states, the detail region, `mark_seen()`                                                                                   |
| `shell/markdown.py`   | theme-safe Markdown with clickable links and local-path underlining                                                                           |
| `shell/format.py`     | `age()`                                                                                                                                       |
| `auth/`, shell wiring | OAuth, credential storage, refresh, seen-state injection — you never touch tokens; `fetch` receives credentials per call and persists nothing |

### What you own

- **`manifest.py`** — id, display name, `ProviderConfig`, `stale_after`,
  declared actions (each tagged `local` / `launch`; `remote` is not
  implemented), and your panel class.
- **`source.py`** — `fetch(credentials, http)` returning your `Item` subclass,
  and `fetch_detail` if your panel shows details. Pagination, filtering, and
  any service quirks are yours on purpose: measured across real MCP servers,
  none of it generalizes (see architecture.md). Sanitize server text with
  `printable_block`, then service-specific normalization, then `capped` — in
  that order.
- **`panel.py`** — extend `Panel`, override its render hooks, and decide your
  policy: grouping, glyphs, ordering, and _when an interaction marks an item
  seen_ (call `self.mark_seen(item)`; skip it and the feature simply doesn't
  exist for your tab).

### Rules the test suite enforces

- **Sources never format; panels never fetch.** The Linear panel carries a
  grep-based test enforcing this (`test_the_panel_never_fetches`); copy it for
  your integration.
- **Errors cross the seam only as `IntegrationError`** — `AuthExpired`
  (re-connect helps), `Unavailable` (retry helps; last-good data shown stale),
  `Malformed` (broken until code changes). Pick by whether retrying helps.
- **Response shape is untrusted.** A server field that should be an object may
  be a string; that must surface as `Malformed`, never a traceback.
- **Reserved keys can't be bound.** The shell owns its keymap
  (`core/keys.py: RESERVED_KEYS`); a manifest binding one is rejected at
  construction.
- **No tokens in output.** Error messages carry no credential material;
  server-controlled text is sanitized before it can reach a terminal.

## Style

- Name intermediates: no resolving and destructuring a value in the same
  expression. More lines beat a dense one-liner.
- Comments and docstrings state the end result, ideally with an input→output
  example. If a comment is needed to explain the mechanism, clarify the code
  instead, then trim the comment. A constraint the code can't show (call
  ordering, a protocol quirk) earns one line.
- Commits: `type(scope): summary`, lowercase, imperative. Bodies only when the
  diff can't say it.

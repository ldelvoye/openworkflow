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

An integration is one directory and one registry line. Read [docs/architecture.md](docs/architecture.md) first — it explains *why* the boundaries below exist.

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

- **`manifest.py`** — id, display name, declared connection paths, `stale_after`, declared actions, and your panel class.
- **`source.py`** — `fetch(credentials, http)` returning your `Item` subclass, and `fetch_detail` if your panel shows details.
- **`panel.py`** — extend `Panel` and override its render hooks. `render_ready()` returns any renderable.

## What the core provides

**Runs for you, no code on your side:**

- Credential storage:
  * For OAuth, the browser login and refresh.
  * For token, masked in-app field.
- Refresh scheduling that follows attention, not a clock.
- Per-tab failure isolation driven by `IntegrationError`
- Seen-state loading and injection, so `mark_seen` below always has a live store to write into.

**Building blocks you call:**

- For MCP connection: `McpSession` (`core/mcp.py`).
- `required_string` / `optional_string` / `timestamp` (`core/shape.py`): untrusted-shape guards that raise `Malformed`.
- `sanitize_line` / `sanitize_block` / `truncate` (`core/text.py`): sanitizing server text.
- `Panel` and its render hooks (`shell/panel.py`).
- `Markdown` (`shell/markdown.py`): theme safe widget with clickable links and local-path underlining.
- `age()` (`shell/format.py`).

**Optional capabilities you opt into:**

- `self.mark_seen(item)`: when an interaction should count as "seen".
- `fetch_detail` (`SupportsDetail` protocol that's feature-detected by the shell): the details pane fetched and cached by the shell. Your panel never touches the network.
- `Action`s: validated against reserved and duplicated keybinds at construction, can be found in the `?` help listing. Action keys should still be bound in `panel.py` as `BINDINGS`.

## What development support you have

**Screenshots.** using `^ + p` in the app. Please attach screenshots when making UI changes for review purposes.

**Sandboxed local runs** by pointing `SMORG_CONFIG_DIR` at a scratch directory and setting `SMORG_CREDENTIAL_STORE=file` to run against it instead of the OS Keychain.

## What is expected: code quality, comment quality, test quality

I don't mind slop code (a lot of the core and auth were made with AI). But enforce the following:
- Use intermediate variables: no resolving and destructuring a value in the same expression (including function calls). More lines is often better than denser statements.
- Use explicit `if/else` to assign variables. Ternaries look ugly in Python and can get really messy.
- Keep comments and docstrings short. Code should be self-explanatory, not everything needs comments (especially in integrations) Ideal docstrings state the end result.

Current state of the test suite might not be ideal. In general though, avoid writing sloppified test cases. Prioritize testing critical paths, and add test cases when fixing issues.

### Rules the test suite enforces

- Sources never format; panels never fetch.
- Errors cross the seam only as `IntegrationError`
- Reserved keys can't be bound.
- No tokens in output.

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

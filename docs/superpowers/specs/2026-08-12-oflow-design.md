# oflow — design

A keyboard-driven terminal dashboard that sits open in a pane and shows what is
on your plate. Each connected integration is a tab with a UI built for that
service. Open source, local only, no daemon.

The repository is `openworkflow`; the distribution, import package, and command
are all **`oflow`** — `openworkflow` is already taken on PyPI by an unrelated
project, and `oflow` is short enough to type daily.

`uvx oflow` works because the distribution name and the console script name are
identical; had they differed, users would need `uvx --from openworkflow oflow`.
First run pays install latency before the screen paints, which for a TUI reads as
a blank terminal — the README says so rather than leaving it a surprise.

## Locked decisions

| Decision        | Choice                                                      | Why                                                                     |
| --------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------- |
| Shape           | Always-open TUI, tab per integration                        | Ambient glanceable view, not a one-shot report                          |
| Stack           | Python + Textual, `uvx`-installable                         | Strongest layout engine for dense multi-panel TUIs; one-line install    |
| Input           | Keyboard only                                               | No mouse affordances anywhere; every action has a key                   |
| Integrations    | Allowlist in the repo, nothing enabled by default           | Unsupported services fail loudly; user opts in per tab                  |
| Transport       | Per integration; MCP for auth, per-server parsing in source | Parse cost ranges from `json.loads` to markdown scraping                |
| Actions         | `local` and `launch` only in v0                             | Read-only scopes; a dashboard is not a client                           |
| Secrets         | OS keychain, explicit opt-in file fallback                  | Never silently write a refresh token in plaintext                       |
| Background work | None in v0 — zero timers                                    | Idle means idle; refresh follows attention, not a clock                 |
| Change signal   | Per-item, keyed off `updatedAt`                             | Highlights what _changed_, so it self-clears instead of nagging forever |

## What MCP actually buys

Two things, and only one of them is uniform across servers.

**Auth — uniform, and the reason MCP is here.** Linear personal API keys are
unavailable in the target workspace, and MCP servers ship OAuth 2.1 with PKCE and
dynamic client registration, so a third-party public client can authenticate
without an admin-issued OAuth app. Linear's `/register` endpoint advertises
S256 PKCE and a `read` scope.

**Data — parseable, but at wildly different cost per server.** Measured
directly, sampling one read-only tool from each:

| Server | Payload                                      | Pagination                              | Cost to consume                  |
| ------ | -------------------------------------------- | --------------------------------------- | -------------------------------- |
| Linear | strict JSON; a `fields` param picks the keys | `hasNextPage` + cursor                  | `json.loads` and a dataclass     |
| Sentry | strict JSON                                  | `hasMore` boolean                       | `json.loads` and a dataclass     |
| Notion | typed JSON envelope, one prose/markup field  | absent, though a sibling tool has it    | thin wrapper, strip one field    |
| Slack  | a markdown document inside a JSON string     | a sentence with the cursor in backticks | regex line-scraping, defensively |

The conclusion is not "MCP output is unparseable" — for Linear and Sentry it maps
straight onto typed records. It is that **parse cost is per-server and spans two
orders of magnitude, so no generic adapter can exist.** A component that turned
any MCP server into a tab would have to be a markdown scraper to handle Slack,
and that scraper would be absurd overhead for Linear. That is why a tab is
defined by an **integration** with its own source module, and why the allowlist
is a feature rather than a limitation.

The real fragility is stability, not shape. Nothing versions these tools: Notion
returns pagination markers from one tool and none from a sibling, Slack ships a
malformed mention token in live output, and Linear deprecated its `/sse` endpoint
on its own schedule. A source module must therefore treat a shape mismatch as a
first-class outcome — hence the `Malformed` error type — rather than assuming
today's response shape holds.

Any integration may use a native API instead where that is better. Note this
escape hatch is likely unavailable for Linear specifically: an MCP-issued token
is scoped to a different audience than Linear's GraphQL API, so MCP is probably
the only door rather than a preference. Worth confirming while building PR 4,
since it decides whether the Linear source has a fallback at all.

The MCP revision we speak, how to recognise that a server has moved past it, and
what upgrading costs, are recorded in `docs/mcp-protocol.md` — a durable document
rather than this one.

## Scope

**v0 — foundation.** Auth, credential storage, the integration contract, the
shell, and one working Linear tab showing a filtered issue list with change
highlighting and manual/on-focus refresh.

**v1 — first feature-rich release.** Opens with the Linear detail pane
(`enter` renders description, state, assignee, and recent comments inline).

**Not in either:** Google Calendar, GitHub, `remote` actions of any kind, mouse
support, desktop notifications, background polling.

## First run

A fresh install opens to an empty shell — no tabs, and a hint to run
`oflow connect linear`. Tabs appear only after being connected and
authenticated, in the order recorded in `config.toml`. Nothing is enabled by
default, and no integration is contactable before its own `connect` has
succeeded.

## Architecture

```
src/oflow/
  cli.py               connect / status / logout / run
  app.py               Textual app: tab bar, global keymap, refresh scheduling
  shell/               shared widgets; the four panel states
  registry.py          integration id -> Integration; the allowlist
  auth/
    oauth.py           OAuth 2.1 + PKCE + dynamic client registration
    store.py           credential storage; the only module that touches tokens
  config.py            config dir, enabled tabs and their order
  state.py             per-item seen state, namespaced per integration
  integrations/
    linear/
      manifest.py      id, display name, auth kind, stale_after, declared actions
      source.py        fetch -> typed items. Never formats.
      panel.py         Textual widget. Never fetches.
```

The load-bearing seam: **sources never format, panels never fetch.** A source is
testable against a recorded payload with no network and no terminal; a panel is
testable against constructed items with no network at all.

`registry.py` is the allowlist. An integration exists only if registered there,
so `oflow connect jira` fails with "not supported" rather than half-working.
Adding an integration is one directory plus one registry line — the open-source
on-ramp.

### The integration contract

| Provides         | Constraint                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------ |
| `manifest`       | id, display name, auth kind, `stale_after`, declared actions each tagged `local` / `launch` / `remote` |
| `authenticate()` | uses the shared OAuth module; never stores credentials itself                                          |
| `fetch(creds)`   | returns typed items; raises a typed error the shell can render                                         |
| `Panel`          | receives state and renders it; may add keys, never rebinds global ones                                 |

Panels differ by nature — a list of tickets and a meeting timeline should not
look alike — but the chrome is shared: tab bar, navigation, refresh, error
states, and highlighting all live in the shell. An integration is a few hundred
lines, not a fork of the UI.

The global keymap is enforced rather than merely documented: shell keys are
declared as App-level `BINDINGS` with `priority=True`, which Textual checks
before the focused widget's bindings and which a widget cannot disable by binding
the same key. Tab switching, `r`, `?`, and `q` therefore work identically on
every tab no matter what a panel declares.

### Actions

Every declared action carries a class, and the class is the safety boundary:

- **`local`** — touches only your own state file (dismiss, pin).
- **`launch`** — leaves the app (open a URL, copy to clipboard).
- **`remote`** — calls someone's API with a write.

v0 ships `local` and `launch`. `remote` exists in the contract as a declared
class so it can be added per integration later, behind confirmation and an
explicit write scope, rather than being retrofitted. No integration can quietly
gain the ability to close your tickets.

## Auth and secrets

Non-secret configuration — enabled tabs, their order, per-integration settings,
and the registered `client_id` (a PKCE public client has no secret) — lives in
`~/.config/oflow/config.toml`, readable and hand-editable.

**Access and refresh tokens go in the OS keychain** via `keyring`: macOS
Keychain, Secret Service on Linux, Credential Manager on Windows. Service
`oflow`, account = integration id.

`keyring` fails loudly by default when no secure backend exists — it resolves to
`backends.fail.Keyring` and raises `NoKeyringError`. That guarantee is not
unconditional: an unrelated `keyrings.alt` install in the same environment gives
it an insecure backend to pick instead. So `store.py` inspects the resolved
backend before writing and refuses any backend not on a known-secure list,
rather than trusting the exception to arrive.

Headless Linux has no Secret Service, so there is an explicit file fallback at
`~/.config/oflow/credentials.json`, enabled only by setting
`OFLOW_CREDENTIAL_STORE=file`. Never silently: a tool that quietly writes your
refresh token in plaintext because a daemon was missing is worse than one that
fails. When that store is active the file is `0600` inside a `0700` directory,
and `oflow` refuses to start if either is wider rather than fixing it silently.

Three rules, all testable:

- **`store.py` is the only module that touches credentials.** Integrations
  receive a token for the duration of a fetch and persist nothing. Refresh is
  central and lock-guarded, so two tabs refreshing at once cannot race and
  invalidate each other's refresh token.
- **Tokens never reach output.** Errors carry a redacted representation and the
  raw value has no `__str__` path. A test renders every error state and asserts
  no token substring appears anywhere.
- **`oflow status` shows liveness, never values** — connected, scope, expiry.
  `oflow logout <id>` deletes from whichever store is active.

Read-only scopes throughout v0, since nothing performs `remote` actions.

## Refresh

**Integrations declare, the shell schedules.** A manifest states what an
integration needs; a single scheduler decides when anything runs. Per-integration
scheduling would make every contributor reimplement backoff, jitter, and
cancellation, and N integrations holding N timers is N wakeups on an idle
machine.

Refresh follows attention, not a clock:

- **Manual `r`** — always available, always forces a fetch.
- **On focus, if stale** — switching to a tab, or the terminal regaining focus,
  fetches only if that tab's data is older than its `stale_after`.
- **Background interval** — deliberately absent in v0.

Terminal focus comes from Textual's `AppFocus` / `AppBlur` events, which fire
with no opt-in wherever the terminal reports FocusIn/FocusOut. tmux forwards them
only with `set -g focus-events on`; where they never arrive, this degrades to
tab-switch and manual, which is still enough.

On startup only the initially visible tab fetches. Other tabs stay unfetched
until first focused, so opening the app costs one request regardless of how many
tabs are configured.

Re-fetching and re-rendering are separate concerns. A live countdown is a local
tick redrawing a number with no network involved. Only the cheap clock ever runs
often, and in v0 neither does: **zero timers, no threads, no polling.** That is
the answer to staying lightweight in Python — not micro-optimization, but having
nothing scheduled when nothing is happening.

## Seen state

The signal worth showing is not "items I have not seen" but **"items that
changed since I last looked at them."** A ticket read yesterday that picked up
three comments overnight should return highlighted; a ticket untouched for a week
should not nag forever.

One stored value per item: the `updatedAt` observed when it was last opened. An
item is highlighted when its current `updatedAt` is newer, or when no value is
stored (a new item). Opening records the new value. This is cheap — one timestamp
per item id, no diffing — and self-clearing, which a plain unread flag is not.
Without it you get a permanently bold inbox where the signal dies from the other
direction. A global "mark all seen" key remains as the escape hatch.

Two ways to open, both counting as having looked: `enter` for the in-TUI detail
pane (v1), and `o` to open in the browser (v0). Seen state is committed only
after a **successful** fetch — committing an empty result from a failed call
would silently mark every real item as already seen.

Per-item seen-on-open is the only mode implemented. A list-level mode may be
added when an integration needs one; one integration does not justify two.

## What the Linear tab shows

Issues assigned to you whose `statusType` is `started` or `unstarted`, sorted by
`updatedAt` descending, grouped by status. Backlog, completed, cancelled, and
triage issues are hidden — unfiltered, most of the list is noise.

Filtering keys off `statusType` (a stable machine category) and presentation off
`status` (a per-team display label teams rename freely), with a fallback glyph for
unrecognised labels. A single account can span many teams with differently named
statuses, and both "In Progress" and "In Review" report as `statusType: started`.
Both fields ship in the response, confirmed against live output, so neither has
to be derived.

`list_issues` takes a `fields` parameter, so the source requests exactly
`title`, `status`, `statusType`, `updatedAt`, `url`, `team`, `assignee`, and
`priority` rather than accepting a default payload. Pagination follows
`hasNextPage` and `cursor`.

`stale_after` is 5 minutes: long enough that switching between tabs does not
refetch constantly, short enough that a glance after stepping away is current.

## Failure semantics

Status is tracked per integration, never globally: one dead API degrades its own
tab and nothing else. A tab has exactly four states, and they must never be
confusable:

| State   | Means                                                           |
| ------- | --------------------------------------------------------------- |
| Loading | A fetch is in flight                                            |
| Empty   | The fetch succeeded and there is nothing assigned to you        |
| Error   | The fetch failed, with the reason shown in place of the content |
| Stale   | Last-good data, marked "as of HH:MM", with the failure noted    |

"Nothing on your plate" and "Linear is down" looking alike is the specific bug
that makes a dashboard untrustworthy.

Errors are typed by what the user would do about them:

- **`AuthExpired`** — offers an inline re-connect prompt.
- **`Unavailable`** — retry; keep last-good data and show it as stale.
- **`Malformed`** — the response did not match the expected shape. The tab is
  broken; say so plainly rather than rendering half of it.

## Testing

- **Sources** against recorded fixtures: no network, no terminal.
- **Panels** against constructed items: no network, one test per failure state.
- **Shell** against a fake integration that can be told to fail on command, so
  error paths are exercised without breaking a real API.
- **Redaction**: render every error state, assert no token substring appears.

## The v0 ladder

Each row is one pull request, reviewable on its own, merging green:

| #   | PR                                                                  | Why it is separable                                          |
| --- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| 1   | Repo scaffold — `pyproject` (uv), ruff, pytest, CI, LICENSE, README | No app logic; establishes the review baseline                |
| 2   | `config.py` — config dir, `config.toml`, enabled tabs and order     | Pure data, fully tested, no network                          |
| 3   | `auth/store.py` — keyring, gated file fallback, perms, redaction    | Security-sensitive; reviewed alone against a fake backend    |
| 4   | `auth/oauth.py` — OAuth 2.1 + PKCE + DCR, loopback callback         | Provider-agnostic; tested against recorded responses         |
| 5   | `registry.py` + contract types — protocol, manifest, error types    | Defines the shape everything downstream implements           |
| 6   | `oflow connect` / `status` / `logout`                               | Auth working end to end, before any UI exists                |
| 7   | `integrations/linear/source.py` + fixtures                          | Data layer only; no UI to review alongside it                |
| 8   | Textual shell — tab bar, global keymap, four panel states           | Driven by a fake integration; reviews without Linear present |
| 9   | `integrations/linear/panel.py` + the `o` launch action              | First real tab; the contract's first consumer                |
| 10  | `state.py` — `updatedAt`-keyed seen state and highlighting          | Behavioral, self-contained                                   |
| 11  | Refresh — manual `r` and on-focus-if-stale                          | Ties it together; nothing before it needs a scheduler        |

PRs 3 and 4 are the auth foundation, and they land before anything can depend on
them being weak. v1 opens with PR 12, the Linear detail pane.

## Config layout

```
~/.config/oflow/
  config.toml        enabled tabs, order, per-integration settings, client ids
  state.json         per-item seen timestamps, namespaced by integration
  credentials.json   only when OFLOW_CREDENTIAL_STORE=file; 0600
```

`OFLOW_CONFIG_DIR` overrides the location.

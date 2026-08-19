# Architecture

smorg is a keyboard-driven terminal dashboard: each connected integration is a
tab, nothing is enabled by default, and the app is read-plus-safe-actions — it
shows what's on your plate and opens things, it never writes to a service.

This document explains the load-bearing decisions. How to *add* an integration
is covered in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Three layers, two seams

```
┌──────────────────────────────────────────────────────────────┐
│ shell/ — chrome every tab inherits                           │
│    app.py — tab bar · keymap · refresh  menu.py  Panel (base)│
└─────────┬────────────────────────────────────────────────────┘
          │ fetch / fetch_detail,                 ▲
          ▼ off the UI thread                     │ extends
┌──────────────────────────────────────────┐      │
│ core/contract.py — Item · Manifest ·     │      │
│ Integration protocol · the three errors  │      │
└─────────┬────────────────────────────────┘      │
          ▼                                       │
┌─────────────────────────────────────────────────┼────────────┐
│ integrations/<id>/ — what an author writes      │            │
│     source.py           manifest.py         panel.py         │
└─────────┬────────────────────────────────────────────────────┘
          │ McpSession · shape guards · sanitizers
          ▼
┌──────────────────────────────────────────────────────────────┐
│ core/ + auth/ — the machine                                  │
│      mcp.py       shape · state · registry · config · auth   │
└──────────────────────────────────────────────────────────────┘
```

- **Integrations** own everything service-specific: fetching, parsing,
  pagination, filtering, and how their tab looks.
- **The shell** owns everything that makes the tabs feel like one program: the
  tab bar, the global keymap, the management menu, refresh scheduling, the
  panel states, the detail region, seen-state injection, and shared rendering
  widgets.
- **Core and auth** are the machine: MCP transport, shape validation,
  sanitizers, seen-state storage, config, credentials.

The seams are enforced, not conventional. The shell reaches an integration only
through the `Integration` protocol; errors cross only as `IntegrationError`
subclasses; neither side imports the other — `core/registry.py` is the only
place integration ids appear. Tests grep for violations ("sources never format,
panels never fetch"), so a broken integration's blast radius is its own tab.

## MCP is the auth layer, not a data contract

MCP appears in this design for one reason: its servers ship OAuth 2.1 with PKCE
and dynamic client registration, so a third-party public client can
authenticate without an admin-issued OAuth app.

Its *data* is another matter. Measured directly across four MCP servers:

| Server | Payload                                      | Pagination                              |
| ------ | -------------------------------------------- | --------------------------------------- |
| Linear | strict JSON; a `fields` param picks the keys | `hasNextPage` + cursor                  |
| Sentry | strict JSON                                  | `hasMore` boolean                       |
| Notion | typed JSON, one prose/markup field           | absent, though a sibling tool has it    |
| Slack  | a markdown document inside a JSON string     | a sentence with the cursor in backticks |

Parse cost spans two orders of magnitude, so **no generic adapter can exist** —
a component that turned any MCP server into a tab would have to be a markdown
scraper to handle the worst case. That is why a tab is an integration with its
own source module, and why the allowlist is a feature: when a server changes
its output, exactly one source breaks, its tab shows the failure, and every
other tab keeps working.

Nothing versions MCP tool output, so every source treats response *shape* as
untrusted alongside content: a field that should be an object may be a string,
and that must degrade one tab (`Malformed`), never crash the app. The protocol
revision we speak, and the upgrade path, live in [mcp-protocol.md](mcp-protocol.md).

## Failure semantics

Status is per-tab, never global. A panel has five states — loading, ready,
empty, error, stale — and empty ("nothing assigned to you") must never look
like error ("the service is down"): a dashboard that blurs them can't be
trusted.

Errors are typed by the one question the shell acts on — **could retrying
help?**

- `AuthExpired` — no; offer a re-connect.
- `Unavailable` — yes; keep last-good data, mark it stale ("as of 14:02").
- `Malformed` — no; the tab is broken, say so plainly.

## Refresh follows attention, not a clock

Zero background timers. Refresh happens on `r`, on switching to a stale tab,
and on the terminal regaining focus — nobody needs fresh data for a tab nobody
is looking at. Only the visible tab fetches at startup, so opening the app
costs one request regardless of how many tabs are configured. Re-rendering
(e.g. a countdown) is a separate, local concern from re-fetching.

## Change marks that clear themselves

The signal worth showing is "changed since you last looked", not "unread". Per
item, the store keeps the `updatedAt` seen when you last opened it; the item is
highlighted when it has moved on since. New items have no stored value and start
highlighted; opening (enter or `o`) records the current stamp; `m` clears the
tab. Keying off change rather than a read flag means an untouched backlog does
not stay bold forever.

## Security posture

- **Tokens live in the OS keychain**, and the store refuses any keyring backend
  not on a known-secure allowlist — an unrelated `keyrings.alt` install would
  otherwise silently supply an insecure one. The plaintext file fallback is
  opt-in only (`SMORG_CREDENTIAL_STORE=file`), never automatic.
- **Permissions are reported, not repaired.** A widened config directory or
  credentials file fails loudly; silently re-tightening would erase the
  evidence that tokens had been readable.
- **The OAuth callback binds an ephemeral port** — nothing can squat a port it
  can't predict — and the server-side registration keeps a stable loopback URI
  (RFC 8252 §7.3 lets servers ignore the port, verified live).
- **Server text is sanitized at the source** (control characters stripped,
  length-capped with a visible marker) and rendered without markup
  interpretation, so a hostile issue title can't restyle the UI or emit
  terminal escapes. Links resolve only from structured server data, https only.
- **Tokens never reach integration code paths' output**: errors carry redacted
  representations, and the serialized credential form never leaves the store
  module — a test fails if any other module references it.

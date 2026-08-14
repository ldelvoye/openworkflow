# Roadmap

What's ahead, as feature sets per release. Design history lives in git;
`docs/mcp-protocol.md` remains the reference for the MCP transport strategy.

## v1.0.0 — fully fledged Linear support, clean integration code

- Detail pane: `enter` renders description, state, assignee, and recent
  comments inline
- Token refresh wired into fetching — central and lock-guarded, expiry checked
  with a clock-skew margin; no more daily reconnects
- Mark-all-seen key
- Unify the panel's plain/styled render paths (with the detail-pane rework)
- Cache the MCP handshake per source instance instead of per refresh

## v1.0.1 — contracts and contributor docs

- Split `ShellKey` and the shell key table out of `contract.py` into their own
  contract module
- Integration-authoring guide: the barebones requirements for adding an
  integration, written to double as an agentic implementation guide

## v1.1.0 — palette feature set

- Index integrations at startup (each integration exposes its auth methods
  through a barrel)
- Palette "add integration": search all integrations, pick a connection path
  (mcp, api, … as declared by the integration)
- Palette "remove integration": search installed integrations, show the
  connection type, confirm, then clean up every piece of persistent state
- Revisit the palette's name once it becomes the management surface

## v1.2.0 — setup and open-source release

- PATH setup: when `oflow` is not on PATH, prompt the user and set it up
- Publish to PyPI so `uvx oflow` works
- `CONTRIBUTING.md` and `SECURITY.md`

## Later, deliberately unscheduled

- GitHub and Google Calendar integrations
- `remote` (write) actions, behind confirmation and explicit write scopes
- Mouse support, desktop notifications, and background polling stay excluded
  by design unless revisited

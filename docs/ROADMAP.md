# Roadmap

What's ahead, as feature sets per release. Design history lives in git;
`docs/mcp-protocol.md` remains the reference for the MCP transport strategy.

## v1.0.1 — contracts and contributor docs

- Split `ShellKey` and the shell key table out of `contract.py` into their own
  contract module

## v1.1.0 — palette feature set

- Index integrations at startup (each integration exposes its auth methods
  through a barrel)
- Palette "add integration": search all integrations, pick a connection path
  (mcp, api, … as declared by the integration)
- Palette "remove integration": search installed integrations, show the
  connection type, confirm, then clean up every piece of persistent state
- Revisit the palette's name once it becomes the management surface
- Fix up the screenshot behavior:
  * Screenshots currently have a different look (same theme, but all the text is more pale)
  * The screenshot notification at the bottom right still shows up in TUI's default dark theme, it should match the user's theme too

## v1.1.1 - bottom banner tweaks

- Change the switch tab reserved keybinds
- Add visual feedback to the refresh keybind

## v1.2.0 — setup and open-source release

- PATH setup: when `oflow` is not on PATH, prompt the user and set it up
- Publish to PyPI so `uvx oflow` works
- `SECURITY.md`

## Later, deliberately unscheduled

- GitHub and Google Calendar integrations
- `remote` (write) actions, behind confirmation and explicit write scopes
- Mouse support, desktop notifications, and background polling stay excluded
  by design unless revisited
- Attachments (e.g. Linear's GitHub PR attachments) as a possible detail-pane
  section — requires fetching the issue's `attachments` field, which the
  current `get_issue` call doesn't request
- An optional config list of extra path roots for the inline-code
  local-path-underline check, for users who run `oflow` from outside the
  workspace root the code paths in an issue are actually relative to

# Roadmap

What's ahead, as feature sets per release. Design history lives in git;
`docs/mcp-protocol.md` remains the reference for the MCP transport strategy.

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
- Enforce using the shift, control, and command symbols instead of `text`
  * This also includes enforcing `+` for any combination (`^p` -> `^ + p`)
- Add visual feedback to the refresh keybind
- Investigate the logic for `seen` and `updated_at`:
  * Is it hard to have a binding to highlight changes?
  * Keep `mark all as seen`, add `mark as unseen`

## v1.2.0 — setup and open-source release

- PATH setup: when `oflow` is not on PATH, prompt the user and set it up
- Publish to PyPI so `uvx smorg` works (includes CI release flow)
- `SECURITY.md`
- Add `CODEOWNERS`, with me as owning everything, and requiring my approval for PRs

## Later, deliberately unscheduled

- GitHub and Google Calendar integrations
- `remote` (write) actions, behind confirmation and explicit write scopes
- Mouse support, desktop notifications, and background polling stay excluded
  by design unless revisited

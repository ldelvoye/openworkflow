# Roadmap

What's ahead, as feature sets per release. Design history lives in git;
`docs/mcp-protocol.md` remains the reference for the MCP transport strategy.

## v1.1.1 - bottom banner tweaks

- Change the switch tab reserved keybinds to h/l
- Enforce using the shift, control, and command symbols instead of text
  * This also includes enforcing `+` for any combination (`^p` -> `^ + p`)
  * This also includes markdown files
- Add visual feedback to the refresh keybind
- Investigate the logic for `seen` and `updated_at`:
  * Is it hard to have a binding to highlight changes?
  * Keep `mark all as seen`, add `mark as unseen`
- Confirm the following v1.1.0 logic:
  * in the menu, what's the logic behind add/remove integration?
    does add only show up when there's available integrations to add?
    does remove only show up when there's available integrations to remove?

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

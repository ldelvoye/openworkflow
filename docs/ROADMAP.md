# Roadmap

What's ahead, as feature sets per release. Design history lives in git;
`docs/mcp-protocol.md` remains the reference for the MCP transport strategy.

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

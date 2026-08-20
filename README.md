# smorg

Short for [Smorgasbord](https://en.wikipedia.org/wiki/Smorgasbord): a table
laid out with many dishes, everyone taking what they want.

smorg is that table for your work — a keyboard-driven terminal dashboard with
one tab per connected integration, showing what's on your plate and what
changed since you last looked. It reads and opens things, never writes; it
refreshes when you look at it, not on a timer. Nothing is enabled by default,
and anyone can bring a dish: an integration is one directory and one registry
line.

## Install

One-off run, no install:

    uvx smorg

That first run installs the package before the screen paints, so expect a
brief blank terminal.

To install it once and reuse it:

    uv tool install smorg
    smorg

Press `^ + p` and pick "Add integration" to connect one, then select the
desired connection method.

`smorg connect <integration>` does the same from the CLI, and is also how you
re-authenticate a tab whose token has expired or been revoked

`smorg logout <integration>` (or "Remove integration" from `^ + p`) removes a tab,
its stored token, and its seen marks.

Inside the dashboard: `h`/`l` switch tabs, `up`/`down` select an item, `o`
opens it in your browser, `r` refreshes, `m` marks the tab's changes seen,
`u` marks the selected item unseen, `?` shows the current tab's keys, `^ + p`
opens the menu (add/remove integrations, screenshots), `q` quits.

## Status

Two dishes on the table:
* **Linear**, showing the issues assigned to you
* **GitHub**, showing what needs your attention

See [docs/ROADMAP.md](docs/ROADMAP.md) for what's ahead.

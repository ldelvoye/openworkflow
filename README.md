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

Not published to PyPI yet. From a checkout:

    uv run smorg run

Then press `ctrl+p` and pick "Add integration" to connect one.
`smorg connect <integration>` does the same from the CLI, and is also how you
re-authenticate a tab whose token has expired. `smorg logout <integration>`
(or "Remove integration" from `ctrl+p`) removes a tab, its stored token, and
its seen marks.

Once released, `uvx smorg` will fetch and run it directly. That first run
installs the package before the screen paints, so expect a brief blank terminal.

Inside the dashboard: `h`/`l` switch tabs, `up`/`down` select an
item, `o` opens it in your browser, `r` refreshes, `m` marks the tab's changes
seen, `?` shows the current tab's keys, `ctrl+p` opens the menu (add/remove
integrations, screenshots), `q` quits.

## Status

Linear is the first dish. See [docs/ROADMAP.md](docs/ROADMAP.md) for what's
ahead.

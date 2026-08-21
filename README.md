# smorg

<!-- demo PR: safe to close/delete, no functional change -->

Short for [Smorgasbord](https://en.wikipedia.org/wiki/Smorgasbord): a table laid out with many dishes, everyone taking what they want.

smorg is that table for your work: a keyboard-driven CLI with one tab per connected integration, showing what's on your plate and what changes since you last looked.

## Install

To install it once and reuse it:

    uv tool install smorg
    smorg

Press `^ + p` and pick "Add integration" to connect one, then select the desired connection method.

`smorg connect <integration>` does the same from the CLI, and is also how you re-authenticate a tab whose token has expired or been revoked

`smorg logout <integration>` (or "Remove integration" from `^ + p`) removes a tab, its stored token, and its seen marks.

Inside the dashboard: `h`/`l` switch tabs, `up`/`down` select an item, `o` opens it in your browser, `r` refreshes, `m` marks the tab's changes seen, `u` marks the selected item unseen, `?` shows the current tab's keys, `^ + p` opens the menu (add/remove integrations, screenshots), `q` quits.

## Status

Three dishes on the table:
* **Linear**, showing the issues assigned to you
* **GitHub**, showing what needs your attention
* **Spotify**, showing your player: now playing, queue, last played

See [docs/ROADMAP.md](docs/ROADMAP.md) for what's ahead.

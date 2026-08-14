# openworkflow

A keyboard-driven terminal dashboard. Each integration you connect becomes a tab.
Nothing is enabled by default.

## Install

Not published to PyPI yet. From a checkout:

    uv run oflow connect linear
    uv run oflow run

Once released, `uvx oflow` will fetch and run it directly. That first run
installs the package before the screen paints, so expect a brief blank terminal.

Inside the dashboard: `shift+←`/`shift+→` switch tabs, `up`/`down` select an
item, `o` opens it in your browser, `r` refreshes, `?` shows the current tab's
keys, `q` quits.

## Status

Pre-alpha. See `docs/superpowers/specs/` for the design.

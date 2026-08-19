# MCP protocol version

`smorg` talks to some services over the Model Context Protocol. MCP is used for
**authentication and transport**, never as a stable data contract — response
shapes vary enormously between servers, so each integration parses its own.

MCP revisions are date strings, and the protocol has changed shape across them.
This document records which revision we speak, how to recognise that it has
stopped working, and what moving costs.

## Where we are

**We speak `2025-11-25`**, verified end to end against Linear's MCP endpoint on
2026-08-13.

The published sequence is:

```
2024-11-05 → 2025-03-26 → 2025-06-18 → 2025-11-25 → 2026-07-28
```

Linear's own preference is `2025-11-25`, so we speak exactly what it prefers and
both of us sit one revision behind current.

**We ask for a version we have tested, not the newest that exists.** A server may
reject an unrecognised version outright rather than negotiating down — Linear
answers HTTP 400 — so optimism breaks connections instead of degrading. The
client then adopts whatever version the server names in its `initialize`
response, which is why that handshake is performed even though a bare
`tools/call` currently works without it.

## Symptoms that we need to move

All of these surface as a broken tab with a message, never a crash. Any one of
them means a server has dropped the revision we speak:

- `initialize` returns a JSON-RPC error, `-32601 Method not found`, or a 404.
  The handshake does not exist after `2025-11-25`, so its absence is the signal.
- A response carries `UnsupportedProtocolVersionError` listing versions that do
  not include ours.
- A `tools/call` returns 400 complaining about the version, or about a mismatch
  between the `MCP-Protocol-Version` header and the request body.

## What moving to `2026-07-28` requires

It is a different protocol rather than a newer dialect, so this is a second
transport path in `src/smorg/core/mcp.py`, not a changed constant:

- **The handshake is gone.** `initialize` and `notifications/initialized` are
  removed; the protocol is stateless.
- **Each request carries its own version.**
  `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` move into
  every request's `_meta`, and the `MCP-Protocol-Version` header must match that
  value exactly or the server returns 400.
- **Two new required headers** on every POST: `Mcp-Method` and `Mcp-Name`.
- **Results gain `resultType`** (`complete` or `input_required`), which callers
  have to branch on.
- **Sessions, the GET SSE endpoint, and `Last-Event-ID` resumability are gone.**
  A broken stream is re-sent as a new request rather than resumed.

## When to do it

When a server we actually target requires it — either Linear drops `2025-11-25`,
or a new integration's server speaks only `2026-07-28`. Not before: a dual path
with no server to test it against is untested code that looks reassuring.

Two shapes are available, and which one fits depends on what the second server
looks like:

| Shape                                              | Cost                                                                               |
| -------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Each manifest declares its server's revision       | No probing, but every integration must know and keep it current                    |
| Attempt the newer protocol, fall back on rejection | Self-configuring, but every connection to a legacy server pays an extra round trip |

**If servers overlap, the upgrade is free.** Because the client adopts whatever
revision `initialize` names, a server that adds `2026-07-28` while keeping
`2025-11-25` needs no change from us at all. Work is only required when a server
_drops_ the old revision.

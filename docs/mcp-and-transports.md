# MCP and transports

How agents talk to Memtrace, and how to pick the right transport for
your situation.

## Background — why two transports

The Model Context Protocol (MCP) is a JSON-RPC dialect Anthropic
introduced for tool calls between AI clients and external services.
The wire shape is the same regardless of how you ship those bytes;
the spec defines two transports:

- **stdio** — the agent spawns the MCP server as a child process and
  pipes JSON-RPC over stdin/stdout. One process per agent session.
- **streamable-HTTP** — the MCP server is a long-running HTTP service
  listening on a port; many agent sessions multiplex through it via
  a session id in headers.

Memtrace supports both. The stdio path has been there from day one.
**Streamable-HTTP is on by default since v0.3.32.**

## Which one should I use?

```
                     ┌────────────────────────────────────────┐
                     │  How many concurrent agents share this │
                     │  Memtrace install?                     │
                     └────────────────────────────────────────┘
                                       │
              ┌────────────────────────┴───────────────────────────┐
              │                                                    │
        1 agent at a time                              many agents (5+)
        (Claude Code, Cursor,                          (Orbit, agent platforms,
         single dev workflow)                           dashboards, CI fleets)
              │                                                    │
              ▼                                                    ▼
        Use stdio.                                   Use streamable-HTTP.
        (the default)                                (one server, many sessions
                                                      multiplexed through it)
```

If you're a regular dev using one editor at a time, **don't change
anything** — stdio Just Works. The rest of this document is for
people building on top of Memtrace.

## stdio transport

### How it looks

```
   ┌─────────────────────┐
   │  Claude Code        │
   │  (or Cursor, Codex, │
   │   Gemini CLI…)      │
   └──────────┬──────────┘
              │ spawns
              ▼
   ┌─────────────────────┐         ┌─────────────────────┐
   │  memtrace mcp       │ ──────▶ │  memtrace start     │
   │  child process      │  gRPC   │  daemon             │
   │  stdio JSON-RPC     │ loopback│  (heavy state)      │
   └─────────────────────┘  :50051 └─────────────────────┘
```

The agent spawns one `memtrace mcp` per session. That child process
attaches to the `memtrace start` daemon over a localhost gRPC loopback
(default `127.0.0.1:50051`) — the daemon owns the heavy state (MemDB,
models, indexes); the child is thin.

When you close your Claude Code window, the child exits. Next session
spawns a new child. The daemon keeps running.

### Setup

Nothing — it's the default. `npm install -g memtrace` writes the MCP
config entry into your client's config file, and the agent picks up
the config on next launch.

### Configuration shape

For tools that don't auto-configure, the entry your config needs is:

```json
{
  "mcpServers": {
    "memtrace": {
      "command": "memtrace",
      "args": ["mcp"]
    }
  }
}
```

That tells the client "spawn `memtrace mcp` as a child and talk to
its stdio". Per-tool config locations:

| Tool | Config file |
|---|---|
| Claude Code | `~/.config/claude-code/mcp.json` (varies by OS) |
| Cursor | `<project>/.cursor/mcp.json` or `~/.cursor/mcp.json` |
| Codex | `~/.codex/mcp.json` |
| Gemini CLI | `~/.gemini/mcp.json` |
| Windsurf | `~/.windsurf/mcp.json` |

The Memtrace installer handles this for you — most users never edit
these files.

## Streamable-HTTP transport

### How it looks

```
   ┌─────────────────────┐
   │  memtrace start     │ ◄────── gRPC loopback :50051
   │  daemon             │
   │  (heavy state:      │
   │   MemDB, models,    │
   │   indexes)          │
   └──────────┬──────────┘
              │
              │ in-process (or separate process)
              ▼
   ┌─────────────────────┐
   │  memtrace mcp       │
   │  MEMTRACE_TRANSPORT │  ◄── ONE long-lived process
   │  =streamable-http   │       Listens on http://localhost:4848/mcp
   │  MEMTRACE_PORT=4848 │
   └──────────┬──────────┘
              │ HTTP JSON-RPC
              │ (session id in header)
              ▼
   ┌─────────────────────────────────────────────────┐
   │  Your orchestrator / proxy / dashboard          │
   │  (Orbit, Memtrace UI, custom MCP gateway, etc.) │
   └─────────────────────────────────────────────────┘
              │
              ▼
   ┌─────────┬─────────┬─────────┬─────────┐
   │ Agent A │ Agent B │ Agent C │ Agent D │ … many concurrent
   └─────────┴─────────┴─────────┴─────────┘
```

One `memtrace mcp` process. One HTTP endpoint. Many concurrent agent
sessions, each with their own session id in the request header.

### Setup

Two env vars on the `memtrace mcp` process:

```bash
MEMTRACE_TRANSPORT=streamable-http MEMTRACE_PORT=4848 memtrace mcp
```

You should see:

```
[memtrace] MCP streamable-HTTP transport listening on http://0.0.0.0:4848/mcp
```

Test it works:

```bash
curl -X POST http://localhost:4848/mcp \
  -H 'Content-Type: application/json' \
  -H 'Mcp-Session-Id: test-1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

A successful response means the transport is up.

### Aliases for compatibility

The MCP spec deprecated plain "SSE" in favour of "streamable HTTP"
(same wire shape, more robust session model). For back-compat,
Memtrace accepts:

| `MEMTRACE_TRANSPORT` value | What it does |
|---|---|
| `streamable-http` | Modern canonical name. Use this. |
| `http` | Short alias for `streamable-http`. |
| `sse` | Legacy alias — also binds streamable-HTTP. Logs a one-line deprecation hint pointing at the new name. |
| `stdio` | Default. Child-process JSON-RPC. |
| (anything else) | Hard error since v0.3.32 — Memtrace refuses to start rather than silently fall back to stdio. |

### Connecting an agent in HTTP mode

Most clients support HTTP MCP via a slightly different config entry:

```json
{
  "mcpServers": {
    "memtrace": {
      "url": "http://localhost:4848/mcp"
    }
  }
}
```

(Exact key may vary per client — Cursor uses `url`, some others use
`type: "http"` + `endpoint`.) Per-client docs evolve quickly; consult
your AI tool's MCP setup guide for the current shape.

### Operational notes

- **Bind address is `0.0.0.0` by default.** If you need to restrict
  to localhost only, run Memtrace inside a container or reverse-proxy
  it. There's no built-in `bind=127.0.0.1` flag yet.
- **Port collisions.** If `MEMTRACE_PORT` is already in use, you'll
  get a clear error pointing at the env var. Pick a free port.
- **Auth.** The HTTP transport doesn't enforce auth at the MCP layer —
  if you're exposing it, put it behind a proxy (Cloudflare tunnel,
  Caddy, nginx) that enforces whatever access control you want.
- **Session lifecycle.** The server keeps in-memory state per
  `Mcp-Session-Id` header. Clean up on the client side when an
  agent disconnects; the server also reaps stale sessions on a
  timer.

## Long-running server pattern (orchestrators)

If you're building a platform on top of Memtrace (orchestrator,
dashboard, agent fleet), the layout you want is:

1. **One `memtrace start` per host.** Owns MemDB, models, watchers.
   Long-lived. Restart only on upgrade.
2. **One `memtrace mcp` per host** with `MEMTRACE_TRANSPORT=streamable-http`
   and a stable `MEMTRACE_PORT`. Long-lived.
3. **Your platform proxies to that endpoint**, multiplexing many
   concurrent agent sessions through it via session ids.

Why not "one `memtrace mcp` per agent session"? You'd be paying a
50–150 ms startup cost per session and re-loading models. With HTTP
the heavy state is loaded once.

Why not "spawn `memtrace mcp` per call"? You'd pay startup on every
single tool call. Even worse than per-session — please don't.

## Building from source with a non-default transport

The streamable-HTTP transport is **on by default** in the npm release
since v0.3.32. If you're building from source and want the smaller
stdio-only binary:

```bash
cargo install --git https://github.com/syncable-dev/memtrace-public \
    --no-default-features
```

This drops the streamable-HTTP code path. The binary will refuse to
start with `MEMTRACE_TRANSPORT=streamable-http` (clear error message),
but stdio works.

## Common questions

### "I see streamable-HTTP, but my legacy code uses `sse`. Will it break?"

No. `sse` is accepted as an alias and maps to the same code path. You
get a one-line deprecation hint logged to stderr; nothing else
changes.

### "Can I run multiple `memtrace start` daemons on one host?"

Yes, but each needs its own `MEMTRACE_MEMDB_LOOPBACK_PORT` and its
own `MEMTRACE_MEMDB_DATA_DIR`. Useful for isolating per-tenant state
on a multi-user box.

### "Can the same `memtrace mcp` process serve both stdio and HTTP?"

No. You pick one transport per process. You can run two `memtrace mcp`
processes — one stdio (for your local agent) and one HTTP (for your
orchestrator) — both attached to the same daemon.

### "Does the stdio child stay running between tool calls within a session?"

Yes. The child stays alive for the lifetime of the agent session. The
agent reuses the open stdio pipe across many `find_code` / `find_symbol`
/ etc. calls. So per-tool-call cost is just the gRPC roundtrip to the
daemon — sub-millisecond on localhost.

### "What happens if the daemon dies while an MCP session is open?"

The MCP child gets a connection error from the gRPC loopback and
returns a clean error to the agent. The next `memtrace start` brings
the daemon back; reopen your editor and the MCP child is fresh too.
No state is lost — the graph is on disk, not in process memory.

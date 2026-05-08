# Getting started

This walks you from "nothing installed" to "your agent is using
Memtrace's knowledge graph". Should take ~5 minutes on a typical
machine.

## What you need

- **Node.js 18 or newer** — Memtrace ships as an npm package that
  bundles a Rust binary for your platform.
- **Git** — used by the indexer to read your repo's history.
- **At least 8 GB RAM** for small/medium projects, **16 GB** if you
  index something the size of Django or Linux. We auto-tune for
  16 GB hosts; tighter machines work but expect to set
  [`MEMTRACE_TIER=light`](environment-variables.md).
- **5 GB free disk** for caches and on-disk graph state. Bigger repos
  push that toward 10–15 GB.
- **macOS, Linux, or Windows.** Everything works on macOS and Linux
  natively. Windows is supported with one caveat — see
  [`troubleshooting.md`](troubleshooting.md#windows-specific-issues).

## Install

### Recommended: npm (one command, all platforms)

```bash
npm install -g memtrace
```

This pulls down the appropriate native binary for your OS+arch
(macOS arm64, macOS x64, Linux x64/arm64, Windows x64), installs the
agent skills the supported AI tools auto-discover, and wires up the
MCP integration for Claude Code, Cursor, and the rest.

After install:

```bash
memtrace --version    # confirms the install
```

### Recommended next step (Apple Silicon especially): warm the embedding model

```bash
memtrace warmup
```

The first `model.embed()` call on Apple Silicon triggers a one-time
CoreML graph compile for the ANE that takes 60–300 s. If you skip this
step and run `memtrace start` first, that compile happens during the
embedding phase — the wait is the same, but the daemon is already
holding open ports and a watcher. Running `memtrace warmup` once after
install gets the cache populated outside the daemon lifecycle so every
subsequent `memtrace start` reaches embedding in single-digit seconds.

Linux and Windows operators can skip this step — the cold-start cost on
non-Apple-Silicon hosts is small enough that the v0.3.84 first-run
detection covers it transparently.

### Cargo (build from source)

If you'd rather build from source — for example to enable a
non-default feature flag — see the public repo at
[`syncable-dev/memtrace-public`](https://github.com/syncable-dev/memtrace-public).
Most users should not need this path.

### Updating

```bash
memtrace install      # pulls latest from npm + chains into your prior command
```

`memtrace install start` upgrades and starts the daemon. `memtrace install index .`
upgrades and re-indexes the current directory.

## First run

Pick a project. From its root:

```bash
memtrace start
```

What happens:

1. **License check.** Memtrace is freeware but requires a session
   token. The first time you run it, you'll be walked through a
   device-flow login (browser opens, you accept, the token caches
   to `~/.memtrace/`). No code or repo data leaves your machine
   during this — see [`privacy-and-telemetry.md`](privacy-and-telemetry.md).
2. **MemDB starts.** Memtrace's embedded knowledge-graph database
   opens its on-disk store at `<project>/.memdb/`. First-time it
   creates the directory; subsequent starts open it in milliseconds.
3. **Models warm up.** On a fresh machine, the embedding model
   (~340 MB ONNX) and the cross-encoder reranker (~75 MB int8) get
   downloaded to `~/.memtrace/` once. After the first machine warm-up
   this is a cache hit on every subsequent start.
4. **Auto-indexing kicks off.** Memtrace walks your repo, parses
   every supported source file (Python, TypeScript, JavaScript, Rust,
   Go, Java, Ruby, C, C++, C#), extracts symbols and relationships,
   and writes them to MemDB. Progress prints to your terminal.
5. **The dashboard goes live.** A local UI at `http://localhost:3030`
   shows you what got indexed, lets you explore the graph, and
   surfaces the value ledger.

For a small repo (mempalace, ~250 files) this completes in under a
second. For Django (~3,300 files) it's around 14 seconds. The numbers
in [`performance-tuning.md`](performance-tuning.md) cover bigger
repos.

## Tell your agent to use Memtrace

If you installed via npm, the integrations for the major AI tools are
already wired:

- **Claude Code** — the MCP server registers automatically. Open a
  Claude Code session in your project root and the tools are there.
- **Cursor** — the per-project MCP config is generated on first run.
- **Codex / Gemini CLI / Windsurf / others** — see
  [`mcp-and-transports.md`](mcp-and-transports.md) for per-tool setup.

To prove it's working, ask the agent something like:

> "Where is the function that handles user login?"

A Memtrace-aware agent answers in one round-trip with `file:line`
locations, not by spelunking through 20 file reads. If you see a
flurry of `Read`/`Grep`/`Glob` calls instead, the MCP isn't wired —
[`troubleshooting.md`](troubleshooting.md#agent-isnt-using-the-mcp).

## Verify the install

```bash
memtrace status
```

Prints something like:

```
  ◆  Memtrace v0.3.32
  ◆  MemDB embedded · data dir = /your/project/.memdb
  ◆  Indexed: 1,234 files · 8,920 symbols · 21,505 edges
  ◆  Models: jina-embeddings-v2-base-code (768d, int8), bge-reranker-base
  ◆  UI: http://localhost:3030
```

If the file/symbol counts are zero on a non-empty repo, indexing
silently failed somewhere — the troubleshooting doc has a checklist.

## What gets created on disk

After your first index:

| Path | What it is | Survives reboot? |
|---|---|---|
| `<project>/.memdb/` | Knowledge graph + vectors for THIS project | Yes |
| `<project>/.memtrace/` | Job state for the indexer (transient) | Yes (small) |
| `~/.memtrace/embed-cache/` | Embedding cache, keyed by symbol AST hash | Yes |
| `~/.memtrace/fastembed_cache/` | Downloaded embedding model files | Yes |
| `~/.memtrace/rerank-models/` | Downloaded cross-encoder reranker files | Yes |
| `~/.memtrace/telemetry/` | Opt-in telemetry buffer (only if you opted in) | Yes |

[`data-directories.md`](data-directories.md) explains each in detail.

## Stopping and resetting

```bash
memtrace stop                  # stop the running daemon
memtrace reset                 # wipe the local MemDB (ALL repos)
memtrace reset <repoId>        # wipe one repo's data only
memtrace start --clear         # wipe and re-index in one go
```

Wiping is non-destructive to your source code — `reset` only deletes
graph data and caches, not files in your repo.

## Where to next

- New to a codebase? → [`workflows.md`](workflows.md#onboarding-to-an-unfamiliar-codebase)
- Building a long-running server / orchestrator on top of Memtrace?
  → [`mcp-and-transports.md`](mcp-and-transports.md#streamable-http-transport)
- Hit an error? → [`troubleshooting.md`](troubleshooting.md)
- Curious what the agent has access to? → [`tools.md`](tools.md)

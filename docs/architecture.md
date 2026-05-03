# Architecture

A user-level picture of how Memtrace fits together. Enough to reason
about behaviour and pick the right knobs — no deep internals.

## The mental model

```
   ┌─────────────────────────────────────────────────────────────┐
   │  YOUR AI TOOL  (Claude Code · Cursor · Codex · Gemini …)    │
   └─────────────────────────────────────────────────────────────┘
                              │ MCP (JSON-RPC)
                              │ stdio  -or-  streamable-HTTP
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  memtrace mcp           — translates MCP calls to graph     │
   │  (a thin process)         queries                           │
   └─────────────────────────────────────────────────────────────┘
                              │ in-process
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  memtrace start         — long-running daemon               │
   │  (the heavy process)    holds:                              │
   │                           · the knowledge graph + vectors   │
   │                           · indexer + file watcher          │
   │                           · embedding model (local ONNX)    │
   │                           · cross-encoder reranker          │
   │                           · full-text (BM25) index          │
   │                           · local UI on :3030               │
   └─────────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  ON-DISK STATE                                              │
   │   <project>/.memdb/      ← per-project graph                │
   │   ~/.memtrace/embed-cache/  ← embedding cache               │
   │   ~/.memtrace/fastembed_cache/ ← model downloads            │
   │   ~/.memtrace/rerank-models/  ← reranker downloads          │
   └─────────────────────────────────────────────────────────────┘
```

## Two processes, one engine

There are exactly **two** things you might run:

### `memtrace start` — the daemon

The heavy process. It:
- Opens the MemDB knowledge graph on disk
- Loads the embedding + reranker models into memory
- Watches your filesystem for changes (`notify` crate)
- Re-indexes incrementally as you edit code
- Serves the local dashboard at `http://localhost:3030`
- Exposes a loopback gRPC endpoint (default `127.0.0.1:50051`)
  for `memtrace mcp` processes to attach to

Run it once per host. It stays alive across editor sessions, terminal
restarts, and CI runs. Stop it explicitly with `memtrace stop` or by
killing the process.

### `memtrace mcp` — the agent's MCP face

A thin process that speaks the Model Context Protocol — JSON-RPC over
either stdio or HTTP. When an agent (Claude Code, Cursor, …) makes a
tool call like `find_symbol`, this process:

1. Parses the MCP request
2. Forwards it to the daemon over a localhost loopback channel
3. Translates the daemon's response back into MCP JSON
4. Streams it to the agent

Spawning a `memtrace mcp` process is cheap (~50 ms) — the heavy
state lives in the daemon. Most users have one `memtrace mcp` per
agent session. Orchestration platforms run a single one in
streamable-HTTP mode and multiplex many agent sessions through it.
See [`mcp-and-transports.md`](mcp-and-transports.md).

## What the daemon actually does

### Indexing

When you `memtrace start` in a new repo (or run `memtrace index <path>`):

1. Walk the filesystem (skipping `.git`, `node_modules`, `target`,
   `dist`, `.claude/worktrees/`, plus anything in `.memtraceignore`).
2. Parse every supported source file — Python, JS, TS, Rust, Go,
   Java, Ruby, C, C++, C#.
3. Extract symbols (functions, classes, methods, structs, etc.) and
   relationships (calls, imports, type references, overrides).
4. Detect HTTP API endpoints (Express, Encore, NestJS, Axum, FastAPI,
   Flask, Gin, Spring Boot, …) and the call sites that hit them —
   cross-service topology for free.
5. Compute graph metrics — PageRank-style centrality, betweenness
   for bridge-symbol detection, Louvain-style modules.
6. Embed the body of every Function / Method / Class / Struct /
   Interface (first ~1500 chars) using a code-specialised model
   (`jina-embeddings-v2-base-code` by default — see
   [`environment-variables.md`](environment-variables.md) for
   alternatives). Embeddings go into an on-disk vector index.
7. Build a full-text index over symbol metadata (name, signature,
   file path, kind) for fast lexical retrieval.
8. Stamp every symbol with `valid_from` / `valid_to` timestamps —
   the bi-temporal layer that powers `as_of` queries and evolution
   tracking.

This is what the daemon does at startup and continuously as files
change. You don't need to trigger anything.

### Searching

When the agent calls `find_code(query="...")`:

1. **Lexical leg** — full-text BM25 search ranks symbols by token
   overlap with per-field boosts (name 5×, signature 3×, etc.).
2. **Semantic leg** — the query embeds; the vector index returns
   nearest neighbours by code-meaning.
3. **Graph leg** — popularity prior (callers) nudges
   well-connected symbols up.
4. **Rank fusion** combines the three rankings.
5. **Cross-encoder rerank** rescores the top 30 candidates and
   returns the top-K (default K=10).

The agent gets `[{file_path, start_line, end_line, name, kind,
score}, ...]` — exact locations, no body unless asked.

### Time travel

Every symbol carries `valid_from` / `valid_to` timestamps tied to a
`git_commit` or `working_tree` (file-save) episode. The agent can ask
`get_evolution(symbol, from=<date>)` and get the full history of
edits, not just the current snapshot. Six scoring modes (impact,
novelty, recency, directional, compound, overview) let agents ask
different temporal questions.

## What the daemon doesn't do

- **It doesn't send your code anywhere.** Indexing, embedding,
  reranking — all local. Only license-validation and (opt-in)
  aggregate telemetry pings cross the network. See
  [`privacy-and-telemetry.md`](privacy-and-telemetry.md).
- **It doesn't depend on a database service.** MemDB is embedded — a
  single binary, no Postgres/SQLite to set up.
- **It doesn't talk to LLM APIs.** Memtrace's pipeline uses only
  local ONNX models. Zero per-query API cost.
- **It doesn't index your dependencies by default.** `node_modules`,
  `target`, `vendor/`, etc. are excluded so the graph stays focused
  on YOUR code.

## How the pieces stay in sync

When a file changes on disk:

1. The file watcher fires.
2. Memtrace re-parses just the changed file.
3. Symbols that disappeared get `valid_to` stamped.
4. New / modified symbols get a fresh `valid_from`.
5. Embeddings only re-run for symbols whose AST hash changed (the
   embed cache catches the rest).
6. Lexical and vector indexes update incrementally.

You don't manually re-index. If the watcher misses a delete (rare —
`rm -rf` of a deep directory can sometimes outpace it), the
[`cleanup_stale_records`](tools.md#cleanup_stale_records) tool
scrubs orphan entries.

## Single-machine vs orchestrator topologies

Most users run one daemon, one agent. Orchestration platforms
(Orbit, agent dashboards) run one daemon and many concurrent agent
sessions through a single `memtrace mcp` HTTP endpoint —
`MEMTRACE_TRANSPORT=streamable-http`. See
[`mcp-and-transports.md`](mcp-and-transports.md).

## What "MemDB" is, briefly

MemDB is the embedded graph engine Memtrace uses — same binary, no
external service to install or run. It stores:

- **Records** (symbols, edges, episodes, vector blobs) keyed by an
  internal record id.
- **Indexes** — fast property lookups, vector nearest-neighbour
  search, per-kind indexes.
- **A write-ahead log** for durability + transactional consistency.

The `.memdb/` directory in your project root is the on-disk form.
Don't edit it by hand; use `memtrace reset` if you want a clean
slate. The on-disk layout is documented at
[`data-directories.md`](data-directories.md).

> **Library terms (only relevant if you're building integrations):**
> the lexical leg is Tantivy, the vector leg uses HNSW, embeddings
> run via the ONNX runtime. You don't need to know any of this to
> use Memtrace — these names exist for people writing custom
> integrations or reading the source.

## Performance expectations

| Operation | What you should see |
|---|---|
| `find_symbol` exact lookup | sub-millisecond |
| `find_code` hybrid retrieval (rerank on) | ~450–900 ms p50 |
| Indexing a small repo (~250 files) | ~0.5 s |
| Indexing a real codebase (~3,300 files, Django) | ~14 s |
| Incremental re-index after one save | ~30–50 ms |
| RSS during normal queries | ~30 MB |
| RSS during indexing (16 GB host) | target ≤ 6 GB |

If your numbers are dramatically off these, the
[`performance-tuning.md`](performance-tuning.md) doc covers the
knobs.

## What to read next

- Want to know exactly what files Memtrace creates? →
  [`data-directories.md`](data-directories.md)
- Want to know exactly what tools your agent gains? →
  [`tools.md`](tools.md)
- Want to plug Memtrace into a service you control? →
  [`mcp-and-transports.md`](mcp-and-transports.md)

# Environment variables reference

Every env var Memtrace reads, what it does, and the default. Grouped
by what you'd reach for them for. Most users never set any of these —
the defaults auto-tune to your machine.

## Quick index

- [Transport + ports](#transport--ports)
- [On-disk locations](#on-disk-locations)
- [Embedding pipeline](#embedding-pipeline)
- [Reranker](#reranker)
- [Search / retrieval tuning](#search--retrieval-tuning)
- [Resource caps](#resource-caps)
- [MemDB connection (advanced)](#memdb-connection-advanced)
- [Telemetry + auth](#telemetry--auth)

## Transport + ports

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_TRANSPORT` | `stdio` | How `memtrace mcp` talks to its agent. See [`mcp-and-transports.md`](mcp-and-transports.md). Values: `stdio`, `streamable-http`, `sse` (alias for streamable-http), `http` (alias for streamable-http). Anything else is rejected with a clear error since v0.3.32. |
| `MEMTRACE_PORT` | `3000` | When transport is HTTP, the port `memtrace mcp` binds. |
| `MEMTRACE_UI_PORT` | `3030` | The local dashboard. Always-on while the daemon is running. |
| `MEMTRACE_WS_PORT` | `3031` | Internal WebSocket bus that pushes index events to the UI. Don't change unless 3031 is taken. |

## On-disk locations

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_DATA_DIR` | `<cwd>/.memtrace` | Per-project indexer state. Override to put it elsewhere. |
| `MEMTRACE_MEMDB_DATA_DIR` | `<repo-root>/.memdb` | The MemDB graph data dir. Anchored to the git root if there is one, otherwise the cwd. Override with an absolute path for shared / CI setups. |
| `FASTEMBED_CACHE_DIR` | `~/.memtrace/fastembed_cache` | Where downloaded embedding models live. |
| `MEMTRACE_DEFAULT_REPO` | (unset) | If set, every tool call without a `repo_id` argument uses this. Convenient for single-repo workflows. |

## Embedding pipeline

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_EMBED_MODEL` | `jina-code` (jinaai/jina-embeddings-v2-base-code, 768d) | Model to use. Other accepted values: `bge-small` (384d, ~140 MB — legacy rollback), `bge-base` (768d, ~440 MB), `nomic` (768d). |
| `MEMTRACE_EMBED_QUANT` | auto-picked from host tier (int8 on Standard, fp32 on Heavy) | Embedding quantisation. Force with `int8` or `fp32`. |
| `MEMTRACE_VECTOR_DIMS` | `768` (matches default model) | Vector dimensionality of the HNSW index. **Must match the model's output dim.** Switching models with a mismatch raises a clear "dim mismatch" error pointing at the right value. |
| `MEMTRACE_EMBED_INTRA_OP_THREADS` | tier-aware (1 / 2 / 4 for Light / Standard / Heavy) | Cap on ORT intra-op threads. Single biggest lever for memory on tight machines. |
| `MEMTRACE_EMBED_BATCH_SIZE` | tier-aware (8 / 16 / 64 for Light / Standard / Heavy) | Per-batch size handed to the embedder. Memory scales linearly with this. Smaller batches finish faster per call, which matters on slow CPU paths (see `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS`). |
| `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` | `60` | Per-batch wall-clock ceiling. When exceeded, the embedding worker is abandoned and respawned on the next call (the bootstrap is self-healing — it just retries). On slow CPU paths (pre-AVX2 hosts: Intel Ivy Bridge / Xeon E5 v2 and older, AMD pre-Excavator) bump to `240` or higher; you'll see the warning `"Embedding batch timed out after 60s …"` if you need it. |
| `MEMTRACE_EMBED_TIMEOUT_DEBUG` | (unset) | Set to `1` to log the offending input previews when a batch times out. Useful for diagnosing whether a single very long symbol body is dragging an otherwise fast batch over the limit. Off by default — these logs include source snippets. |
| `MEMTRACE_EMBED_RSS_LIMIT_GB` | tier-aware (3 / 6 / 10 / 20 GB) | Soft RSS ceiling that triggers back-pressure when exceeded. Set to `0` to disable the check. |
| `MEMTRACE_EMBED_MIN_LINES` | `4` | Don't embed symbols with fewer body lines than this. Prevents wasting cache on trivial helpers. |
| `MEMTRACE_DISABLE_COREML` | (unset) | Set to `1` on Apple Silicon to force CPU execution provider instead of CoreML / ANE. Useful if CoreML's first-run graph compile hangs on your machine. |
| `MEMTRACE_TIER` | auto-detected (`light` / `standard` / `heavy`) | Force the host tier instead of letting Memtrace pick from RAM + CPU + accelerator signals. |

## Reranker

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_RERANK` | `on` | Enable / disable cross-encoder rerank in `find_code`. Set to `off` for a pure BM25 + vector pipeline (~3–4 pp lower acc@1 but ~400 ms faster per query). |

## Search / retrieval tuning

You typically don't need these. They exist for benchmarking and for
edge cases where you want to bias the ranking yourself.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_RRF_K` | `60` | Reciprocal Rank Fusion constant. Smaller = more aggressive top-K; larger = flatter. |
| `MEMTRACE_RRF_BM25_WEIGHT` | `2.0` | Weight on the BM25 leg in RRF. |
| `MEMTRACE_RRF_VECTOR_WEIGHT` | `1.0` | Weight on the vector leg. |
| `MEMTRACE_RRF_GRAPH_WEIGHT` | `0.75` | Weight on the graph signal. |
| `MEMTRACE_RRF_EXACT_WEIGHT` | tuned default | Weight applied to exact-name matches before fusion. |

## Resource caps

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_MAX_THREADS` | `n-2` for `memtrace index`, `n/2` for `memtrace start` | Rayon parse-thread pool size. Useful to leave cores free for your editor. |

## MemDB connection (advanced)

You almost certainly don't touch these. They exist for people running
Memtrace against a remote MemDB cluster instead of the embedded
default.

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_MEMDB_MODE` | `embedded` (auto-detected from `MEMDB_ENDPOINT` if set) | `embedded` or `remote`. |
| `MEMTRACE_MEMDB_ENDPOINT` | `http://127.0.0.1:50051` | Remote MemDB gRPC endpoint. Setting this to anything other than the loopback default flips mode to `remote`. |
| `MEMTRACE_MEMDB_LOOPBACK_PORT` | `50051` | When `memtrace start` runs in embedded mode, the loopback gRPC port the in-process MemDB binds. Other processes (`memtrace mcp`, MemFleet broker) attach here. Set to `0` for ephemeral. |
| `MEMTRACE_MEMDB_DB` | `memtrace` | The database name within MemDB. |
| `MEMTRACE_MEMDB_AUTH_TOKEN` | (unset) | Bearer token for `remote` mode. Ignored for `embedded`. |

## Telemetry + auth

| Var | Default | Purpose |
|---|---|---|
| `MEMTRACE_TELEMETRY_DISABLED` | (unset) | Set to `1` to block telemetry regardless of consent state. Hard override. |
| `MEMTRACE_NO_REMOTE_RECEIPT` | (unset) | Set to `1` to omit the weekly-receipt symbol-name surface from heartbeats. Even if the user opted in to weekly emails on memtrace.io, this env var ensures no symbol names cross the network from this machine. The cloud then has nothing concrete to anchor an email and skips the send for that week. See [`privacy-and-telemetry.md`](privacy-and-telemetry.md#4-weekly-memtrace-receipt-opt-in-off-by-default). |
| `MEMTRACE_LICENSE_KEY` | (unset) | Optional bearer-style license key for non-interactive (CI / server) authentication. Most users authenticate via device flow on first run instead. |

## Redis / pub-sub (multi-process deployments)

Memtrace can broadcast indexing events to a Redis channel so
detached UIs and orchestrators can subscribe without polling.

| Var | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (unset) | Where to publish `memtrace:indexed` events. Empty = no publish; in-process WebSocket is still used. |
| `VALKEY_URL` | (unset) | Same as `REDIS_URL`. Convenience alias if you're on Valkey. |

## Internal / undocumented

There are a few env vars used by tests and CI that aren't part of the
stable API and may change without notice. They're all prefixed
`MEMTRACE_TEST_*` or `RUST_LOG`. Don't depend on them in production.

## Examples

A 16 GB laptop running on battery, wanting maximum thrift:

```bash
export MEMTRACE_TIER=light
export MEMTRACE_RERANK=off
memtrace start
```

A 64 GB workstation building agent infra, wanting maximum speed and
recall:

```bash
export MEMTRACE_TIER=heavy
export MEMTRACE_TRANSPORT=streamable-http
export MEMTRACE_PORT=4848
memtrace start
memtrace mcp     # binds the HTTP server on :4848
```

A CI pipeline that needs deterministic behaviour:

```bash
export MEMTRACE_LICENSE_KEY=<your CI key>
export MEMTRACE_TELEMETRY_DISABLED=1
export MEMTRACE_DISABLE_COREML=1   # macOS CI runners
memtrace index .
```

A small Raspberry Pi 4 (4 GB) — tight RAM, no rerank:

```bash
export MEMTRACE_TIER=light
export MEMTRACE_EMBED_MODEL=bge-small        # smaller model, 384d
export MEMTRACE_VECTOR_DIMS=384
export MEMTRACE_RERANK=off
export MEMTRACE_EMBED_BATCH_SIZE=4
memtrace start
```

See [`performance-tuning.md`](performance-tuning.md) for more recipes.

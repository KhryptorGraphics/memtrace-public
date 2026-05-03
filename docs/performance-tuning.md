# Performance tuning

Fitting Memtrace to your machine. The defaults auto-tune from
detected RAM + CPU + accelerator signals, so most users never touch
these knobs. This doc is for when the defaults aren't right for you.

## Glossary (read this first if the rest looks like jargon)

A few terms come up repeatedly in this doc. Each is plain enough
once you've seen it once.

- **ONNX runtime** — the local inference engine that runs the
  embedding and rerank models. No network calls, no GPU required.
- **Intra-op threads** — how many CPU threads the ONNX runtime uses
  for one operation (a matrix multiply, etc.). More threads = faster
  but more memory per op. The single biggest lever for memory on
  tight machines.
- **Batch size** — how many symbols Memtrace embeds in one go.
  Memory scales linearly with this.
- **RSS** — Resident Set Size, the actual amount of physical RAM the
  process is holding. Shown by Activity Monitor on macOS, `htop` on
  Linux. The "RSS guardrail" is a soft ceiling that triggers
  back-pressure during indexing.
- **CoreML / ANE** — Apple's on-chip accelerator for ML inference.
  Memtrace uses it on Apple Silicon by default; turning it off (`MEMTRACE_DISABLE_COREML=1`)
  forces CPU-only inference, which is slower but avoids first-run
  graph-compile delays.

## How auto-tuning works

On startup, Memtrace classifies your host into one of three tiers
based on a 0–11 score combining:

- **RAM** — 0 pts (<12 GB) → 5 pts (≥48 GB)
- **CPU** — 0 pts (≤3 cores) → 4 pts. Apple Silicon variants get
  a fixed table: M-base = 1, M-Pro = 2, M-Max = 3, M-Ultra = 4.
- **Accelerator** — discrete GPU = 2 pts, Apple Neural Engine = 1 pt

```
   score   tier         picks            embed quant
   ─────   ──────────   ──────────────   ──────────────
   0–2     light        small batches    int8
   3–6     standard     medium batches   int8
   7+      heavy        large batches    fp32
```

The actual tuning happens via three `RuntimeProfile` accessors:

| Setting | Light | Standard | Heavy |
|---|---|---|---|
| `embed_intra_op_threads` | 1 | 2 | 4 |
| `embed_batch_size` | 8 | 16 | 64 |
| `embed_rss_limit_gb` | 3–4 | 6 | 10–20 |

Override anything with `MEMTRACE_TIER=light|standard|heavy` (forces
the tier) or by setting the individual env vars in
[`environment-variables.md`](environment-variables.md).

## Common scenarios

### "16 GB M1/M2/M3 Pro — Memtrace is eating my RAM"

This was the v0.3.30-and-earlier failure mode: 27+ GB resident
during indexing. The fix shipped in v0.3.31:

- ORT intra-op threads capped to 2 (was: num_cpus = 10)
- Embed batch capped to 16 (was: 128)
- RSS guardrail at 6 GB triggers back-pressure

**If you're on v0.3.31+ and still seeing high RSS:**

```bash
# Force the tier even tighter
export MEMTRACE_TIER=light

# Lower batch further (extreme)
export MEMTRACE_EMBED_BATCH_SIZE=4
export MEMTRACE_EMBED_INTRA_OP_THREADS=1

# Tighter RSS ceiling
export MEMTRACE_EMBED_RSS_LIMIT_GB=4

# Drop the rerank model to save ~250 MB resident
export MEMTRACE_RERANK=off

memtrace stop && memtrace start
```

Watch for the log line:

```
embed: RSS sample batch_idx=32 rss_mb=<N> limit_mb=<L>
```

If `rss_mb` stays under your `limit_mb`, you're in good shape.

### "8 GB laptop / Raspberry Pi — even bge-small is too big"

Drop to the smallest sensible config:

```bash
export MEMTRACE_TIER=light
export MEMTRACE_EMBED_MODEL=bge-small         # 384d, ~140 MB resident
export MEMTRACE_VECTOR_DIMS=384               # MUST match the model
export MEMTRACE_EMBED_QUANT=int8
export MEMTRACE_EMBED_BATCH_SIZE=4
export MEMTRACE_EMBED_INTRA_OP_THREADS=1
export MEMTRACE_RERANK=off
export MEMTRACE_DISABLE_COREML=1              # if on Apple Silicon Pi-equivalent
memtrace start
```

You'll lose ~6 pts of acc@1 vs the default jina model on agent-style
queries. Tradeoff is intentional — fits in 4 GB hosts.

### "Workstation with 64 GB + a discrete GPU — go fast"

The defaults already pick `Heavy` tier on this host. If you want
even more throughput:

```bash
export MEMTRACE_TIER=heavy
export MEMTRACE_EMBED_BATCH_SIZE=128          # bump from 64 default
export MEMTRACE_EMBED_INTRA_OP_THREADS=8      # bump from 4 default
export MEMTRACE_EMBED_QUANT=fp32              # already default on Heavy
memtrace start
```

For multi-tenant (orchestrator) deployments, bind streamable-HTTP
and let many agents share one daemon — see
[`mcp-and-transports.md`](mcp-and-transports.md).

### "Indexing Django takes 14s — I want it faster"

Most of indexing time on big repos is the embedding pass, not the
parser. To skip embedding entirely (you lose `find_code` semantic
search but keep BM25, structural search, and time travel):

```bash
export MEMTRACE_EMBED_MIN_LINES=1000          # de facto skip everything
memtrace index <path>
```

To use a smaller, faster embedding model:

```bash
export MEMTRACE_EMBED_MODEL=bge-small
export MEMTRACE_VECTOR_DIMS=384
memtrace reset && memtrace index <path>
```

bge-small is ~3× faster to embed than jina-code at the cost of
~6 pts retrieval accuracy.

### "Rerank takes 400ms per query — I want faster queries"

```bash
export MEMTRACE_RERANK=off
memtrace stop && memtrace start
```

You'll get ~50–150 ms p50 instead of ~450–870 ms, with
~3–4 pp lower acc@1 on agent-style queries. Worth it for
auto-completion-style use cases; not worth it when correctness
matters.

### "Re-indexing the same repo over and over (in CI)"

Take advantage of the embed cache — `~/.memtrace/embed-cache/` is
keyed by symbol AST hash, so unchanged symbols are cache hits even
on a fresh `.memdb/`.

If you nuke `.memdb/` in CI but keep `~/.memtrace/embed-cache/`
mounted as a volume, the embed pass becomes nearly free. The first
CI run is slow; everything after is fast.

## What "auto-tuned for your host" actually decides

When you run `memtrace start`, the banner shows:

```
  ◆  Host profile: Apple M3 Pro · 12 (6P+6E) · 18 GB · score=5 · tier=standard · embed=int8
```

Decoded:
- M3 Pro CPU
- 12 cores total, 6 performance + 6 efficiency
- 18 GB RAM
- score 5 → Standard tier
- int8 embedding quantisation

If the auto-pick is wrong for your situation, override with
`MEMTRACE_TIER=...`.

## Specific knobs and their cost / benefit

### `MEMTRACE_EMBED_BATCH_SIZE`

| Value | RAM impact | Throughput | Best for |
|---|---|---|---|
| 4 | ~30% lower than default | ~30% slower | RPi / very tight RAM |
| 8 | tier `Light` default | baseline | M1/M2 8 GB |
| 16 | tier `Standard` default | baseline | M1/M2/M3 16 GB |
| 32 | ~30% higher than default | ~10–15% faster | 32 GB workstation |
| 64 | tier `Heavy` default | best | 32+ GB workstation |
| 128 | extreme | marginal gain | 64+ GB GPU box |

Memory is `O(batch × seq_len × hidden × per_thread_scratch)`. The
sweet spot is "as large as fits without swapping".

### `MEMTRACE_EMBED_INTRA_OP_THREADS`

ORT spawns intra-op threads for parallel MatMul. Each thread holds
its own scratch buffers, so doubling threads ~doubles the per-op
RAM. Default is 2 on Standard hosts. Going above 4 on a non-GPU
host rarely helps (memory-bandwidth-bound, not CPU-bound).

### `MEMTRACE_RERANK`

| Setting | acc@1 (Django, agent queries) | p50 latency |
|---|---|---|
| `off` | ~70% | ~50 ms |
| `on` (default) | ~74% | ~450 ms |

The reranker holds an extra ~75 MB resident (int8 model). For most
agent workflows the +4 pp accuracy is worth it; for sub-100ms
auto-completion paths, turn it off.

### `MEMTRACE_EMBED_RSS_LIMIT_GB`

A soft ceiling. When the daemon's RSS crosses it during indexing,
the embed loop yields for 50 ms and logs a warning. This lets the
MemDB writer drain in-flight batches and gives the allocator a
chance to return pages.

Don't set this so low that it fires constantly — that just throttles
indexing. The default scales with host RAM and is generally right.

## Diagnosing without changing anything

```bash
memtrace status                    # high-level: data dir, counts, models
```

In the daemon's log (run `memtrace start` in foreground or
`tail -f ~/.memtrace/logs/...`), look for:

```
ort: global thread pool capped — intra_op=2, inter_op=1
embed: RSS sample batch_idx=32 rss_mb=4892 limit_mb=6144
```

Those tell you the auto-tuner kicked in and the embed loop is
respecting the RSS budget.

For per-query latency, the local UI at `localhost:3030` has a Value
Ledger panel that breaks down where time goes.

## When something is genuinely too slow

Open a GitHub issue with:

1. `memtrace status` output
2. Your `memtrace --version`
3. The host profile line from the daemon banner
4. A short repro — what command, what repo size, what observed time

We tune based on real workloads, not synthetic ones. Field reports
move the defaults faster than anything else.

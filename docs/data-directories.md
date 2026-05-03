# Data directories

Every directory Memtrace creates, where it lives, what's inside, and
when (if ever) to delete it.

## TL;DR map

```
   YOUR PROJECT
   ├── .memdb/           ← graph + vectors for THIS project (per-repo)
   └── .memtrace/        ← indexer job state (transient, small)

   YOUR HOME
   ~/.memtrace/
   ├── embed-cache/      ← per-symbol embedding cache (cross-project)
   ├── fastembed_cache/  ← downloaded embedding models
   ├── rerank-models/    ← downloaded reranker model
   ├── auth/             ← session tokens (one file)
   └── telemetry/        ← buffered events (only if telemetry is ON)
```

Two important rules:
- **Per-project state lives in your project.** That makes copying /
  archiving / nuking a project's graph trivial — `rm -rf .memdb` is
  always safe.
- **Cross-project state lives in `~/.memtrace/`.** Models, embedding
  cache, and your session token aren't tied to a single repo.

## Per-project: `<project>/.memdb/`

The MemDB graph engine's on-disk store. Created the first time you
run `memtrace start` or `memtrace index <path>` from your project
root.

Layout (high-level — the inner files are MemDB's business; don't
touch them):

```
.memdb/
└── memtrace/                     # database name
    ├── wal/                      # write-ahead log
    ├── episodes/                 # commit + working-tree snapshots
    ├── paged-records/            # Nodes / Edges / Episodes / VectorBlobs
    ├── indexes/                  # property indexes + HNSW vectors
    ├── tantivy/                  # BM25 full-text segments
    └── manifest.toml             # MemDB metadata
```

Size grows roughly linearly with your codebase. Some rough numbers:

| Project | Files | Symbols | `.memdb/` size |
|---|---|---|---|
| Small (mempalace) | ~250 | ~1.8k | ~30 MB |
| Medium (Express, Bun-style) | ~800 | ~12k | ~120 MB |
| Large (Django) | ~3,300 | ~50k | ~700 MB–1 GB |
| Huge (Linux kernel-class) | 30k+ | 500k+ | 5–10 GB |

**Override location** with `MEMTRACE_MEMDB_DATA_DIR=<absolute path>`
if you want it outside the repo (e.g. on a faster disk).

**When to delete:** `memtrace reset` does this safely. Manual
`rm -rf .memdb/` works too if the daemon isn't running. The next
`memtrace start` will re-index from scratch.

## Per-project: `<project>/.memtrace/`

Job state for the indexing pipeline — progress, watchers, recovery
metadata. Tiny (usually < 1 MB). Created the first time the daemon
runs in your project.

If the daemon crashes mid-index, this is what lets it resume rather
than starting over. Safe to delete when the daemon isn't running; you
just lose the resume point and the next `memtrace start` re-indexes
from scratch.

**Override location** with `MEMTRACE_DATA_DIR=<path>`.

## `<project>/.memtraceignore`

Optional file. Glob patterns of paths the indexer should skip,
on top of the built-in exclude list (`.git`, `node_modules`,
`target`, `dist`, `build`, `.venv`, `vendor`, `.claude/`, etc.).

```
# .memtraceignore — same syntax as .gitignore
docs/generated/
**/*_pb2.py
fixtures/
```

You usually don't need this — the built-in excludes cover most cases.
Reach for it when a generated/vendored directory is bloating your
graph.

## `~/.memtrace/embed-cache/`

A redb-backed key-value store mapping `(model_id, symbol_ast_hash)`
→ embedding vector. **Cross-project** — re-indexing a different repo
that has the same symbol body doesn't recompute the embedding.

Layout:
```
~/.memtrace/embed-cache/
└── memtrace_embed_v2.redb       # single file, ACID, mmap
```

Typical size: 200 MB–2 GB depending on how many distinct symbols
you've indexed across all your projects.

**When to delete:** Only when you change embedding models. The cache
is keyed by model ID, so switching from `jina-embeddings-v2-base-code`
to `bge-small` makes the existing entries cache-misses anyway — but
they still take disk. Manual cleanup:

```bash
rm -rf ~/.memtrace/embed-cache/
```

## `~/.memtrace/fastembed_cache/`

HuggingFace-style cache for the downloaded embedding model. Default
is `jina-embeddings-v2-base-code` (~340 MB f32 ONNX, ~85 MB int8).

Layout:
```
~/.memtrace/fastembed_cache/
├── models--jinaai--jina-embeddings-v2-base-code/
│   ├── snapshots/<sha>/
│   │   ├── model.onnx           OR model_int8.onnx
│   │   ├── tokenizer.json
│   │   └── tokenizer_config.json
│   ├── blobs/                   # Hugging Face content-addressed blobs
│   └── refs/main
└── models--Xenova--bge-small-en-v1.5/    # only if you've used bge-small
```

Size: 340–500 MB for the default model. Adding alternative models
(bge-small, bge-base) costs 100–500 MB each.

**Override location** with `FASTEMBED_CACHE_DIR=<path>` — useful if
your home directory is on a small SSD and you want models on a
different disk.

**When to delete:** When you want to redownload a model (rare). The
cache is content-addressed, so a partial download self-heals on next
use.

## `~/.memtrace/rerank-models/`

Cross-encoder reranker models. Default is `BAAI/bge-reranker-base`
(int8 quantized, ~75 MB).

Layout:
```
~/.memtrace/rerank-models/
└── bge-reranker-base/
    ├── model_int8.onnx
    ├── tokenizer.json
    └── config.json
```

The reranker is loaded into memory only when `MEMTRACE_RERANK=on`
(the default). Disable it with `MEMTRACE_RERANK=off` if you want a
pure BM25 + vector pipeline (faster, ~3–4 pp lower acc@1 on typical
agent queries).

## `~/.memtrace/auth/`

Your Memtrace session token, refreshed automatically. One file:

```
~/.memtrace/auth/
└── session.json     # { device_id, token, expires_at }
```

Don't share this file. If you suspect it's leaked,
`memtrace auth logout` deletes it; the next `memtrace start` walks
you through device-flow login again.

## `~/.memtrace/telemetry/`

Only created if you opted into telemetry during `memtrace start`.
Stores a small batch of pending events to be sent on the next
heartbeat. See [`privacy-and-telemetry.md`](privacy-and-telemetry.md)
for what's actually in there.

If you didn't opt in, this directory doesn't exist.

## Things Memtrace creates outside its own directories

A few files end up in your repo or home folder beyond the four
directories above:

- **Skills** are installed at the global path your AI tool expects
  (e.g. `~/.claude/skills/memtrace-skills/...` for Claude Code,
  `.cursor/...` for Cursor). Generated by `memtrace install` /
  `npm install -g memtrace` postinstall.
- **MCP config entries** are appended to your tool's config:
  `~/.config/claude-code/mcp.json`, `~/.cursor/mcp.json`, etc. The
  installer is idempotent — running it twice doesn't duplicate
  entries.
- The Memtrace npm shim itself lives wherever your global npm puts
  things (`~/.npm-global/lib/node_modules/memtrace/` on most setups).

## Cleaning up everything

Full reset to factory:

```bash
memtrace stop                         # stop the daemon
rm -rf <project>/.memdb               # this project's graph
rm -rf <project>/.memtrace            # this project's job state
rm -rf ~/.memtrace                    # ALL machine-level state
npm uninstall -g memtrace             # the binary + skills
```

After this Memtrace leaves no trace on your system. Your source code
is never touched.

## What's safe to delete during normal use

| Path | Safe? | Effect |
|---|---|---|
| `<project>/.memdb/` | Yes (daemon stopped) | Re-index from scratch on next start |
| `<project>/.memtrace/` | Yes (daemon stopped) | Lose resume point; re-index full on next start |
| `~/.memtrace/embed-cache/` | Yes any time | Re-embed symbols on next index |
| `~/.memtrace/fastembed_cache/` | Yes any time | Re-download model on next start (~340 MB) |
| `~/.memtrace/rerank-models/` | Yes any time | Re-download reranker (~75 MB) |
| `~/.memtrace/auth/` | Yes | Forces re-login on next start |
| `~/.memtrace/telemetry/` | Yes | Drops any unsent events |

Nothing here is precious. The graph rebuilds itself; the caches
warm themselves; the auth re-authenticates. Memtrace is designed so
you can `rm -rf` any of these at any time without consulting docs
first.

# LeanCTX Native — compressed reads, smart trees, and a value ledger

**Available in v0.3.57 and later. Default-on except where noted.**

Memtrace's MCP surface used to give agents two superpowers: **find the right symbol** (graph queries — `find_code`, `find_symbol`, `get_symbol_context`, `get_impact`) and **read it precisely** (`get_source_window` returning raw source bytes for the exact line range).

The graph layer routinely saves 80%+ of an agent's tokens vs. raw file reads. But the source layer was returning every comment, every blank line, every import block — bytes the agent rarely needed. **LeanCTX Native** closes that gap. Memtrace now compresses what it returns, maps directories in one call, and surfaces the savings in real time.

The full feature set landed across four versions (v0.3.52 → v0.3.57) and is now treated as one capability.

---

## What ships in LeanCTX Native

### 1. Compressed source reads

`get_source_window` accepts a `mode` parameter:

| Mode | What it returns | Typical reduction |
|---|---|---|
| `raw` *(default)* | Verbatim bytes — unchanged from earlier versions | 0% (baseline) |
| `lightweight` | Whitespace + blank lines collapsed | ~10-30% |
| `aggressive` | Comments stripped, boilerplate collapsed, import blocks elided past the first N | ~70-95% |
| `map` | Function signatures + class headers only — no bodies | ~95-99% |

```jsonc
// Agent asks for a function in compressed form
{
  "tool": "get_source_window",
  "args": {
    "symbol_id": "memdb:1234",
    "mode": "aggressive"
  }
}
```

When `mode` ≠ `raw`, the response includes byte counters so you can audit per-call savings:

```jsonc
{
  "body": "fn validate_token(req: Request) -> Result<Claims> { ... }",
  "original_bytes": 12340,
  "compressed_bytes": 1480,
  "ratio": 0.12,
  "mode": "aggressive",
  "_meta": {
    "context_avoided_bytes": 10860
  }
}
```

The `_meta` field appears **only** on `get_source_window` calls with non-raw modes. Every other MCP tool's response shape is byte-identical to v0.3.51 — adding `_meta` to every response would have been agent-context pollution for no agent benefit, so the per-call meta lives only where the agent explicitly opted into compression.

**Backwards-compat:** if you don't pass `mode`, you get `raw` and the response shape is unchanged from earlier versions. Existing integrations break nothing.

### 2. `get_directory_tree` — single-call repo map

Agents exploring an unfamiliar repo used to make 10-20 file-listing calls to build a mental map. Now it's one call:

```jsonc
{
  "tool": "get_directory_tree",
  "args": {
    "repo_id": "my-app",
    "max_depth": 4,
    "max_entries_per_dir": 30,
    "mode": "compact"
  }
}
```

Three modes:

- **`compact`** *(default)* — collapses single-child chains (`src/server/auth/handlers/login.ts` with no siblings → one line)
- **`verbose`** — every directory expanded, no collapse
- **`map`** — compact + inline a function-signatures-only summary of the central file in each directory (uses the same `mode: "map"` machinery from #1)

**Bounded by design.** At default caps, the response is hard-bounded to ≤ 4 KB regardless of repo size. When the per-dir entry cap or byte budget elides paths, the response carries `_truncated_paths: [string]` so the agent can drill in with a follow-up call against a higher cap on a specific subtree.

**Sourced from the indexed graph, not the filesystem.** `.gitignore` and `.memtraceignore` rules inherit automatically — the indexer already filtered them, so the tree never includes `node_modules`, `dist`, `target`, etc.

### 3. Server-side token-savings ledger

Every tool dispatch now records `bytes_avoided` to a persistent ledger in MemDB. The dashboard's Value panel surfaces:

- **Per-session totals** — running bytes saved + dollar equivalent
- **Live trace** — last 20 tool calls with bytes saved per call
- **Aggregate** — total bytes / dollars saved across the session, lifetime, or filtered by repo

The `tool_call_recorded` WebSocket event broadcasts on every call, so the dashboard updates in real time without polling.

**Important:** the ledger is server-side only. **No tool emits a `_meta.context_avoided_bytes` envelope on its response except `get_source_window` non-raw modes** (the explicit-opt-in case). Modern agents don't self-regulate on per-call meta fields, so adding bytes to every response would have been pollution for no agent benefit. The savings information lives where humans see it (the dashboard) and where systems can act on it (Phase 4's bandit), not where it would crowd the agent's context window.

### 4. Adaptive mode selection (Preview Mode — opt-in)

When the agent calls `get_source_window` without specifying a `mode`, what should Memtrace pick?

The answer used to be a hardcoded `(language → mode)` table — `.rs` Medium → Map, `.md` → Aggressive, etc. That table is right ~60-70% of the time on real repos. When it's wrong, the agent re-reads with a wider mode and the token win evaporates.

**LeanCTX Native ships an adaptive learner that picks per-`(language, file_size_bucket, agent_pattern)` slot based on real agent behavior.** Thompson-sampling bandit. Trained on the ledger's call sequences. Reward: -1 if the agent re-called the same file with a wider mode within 5 turns; otherwise `+min(bytes_avoided / 1000, 10)`.

Our internal benchmark shows **+14.29% additional reduction** on top of static mode selection, after the bandit has seen ≥ 50 samples per slot.

#### Preview Mode — why it's opt-in

The bandit is **off by default in v0.3.57.** Operators opt in by setting:

```bash
export MEMTRACE_ADAPTIVE_MODES=1
```

Why preview mode:

1. **The bandit needs ≥ 50 samples per slot before it converges.** Below that threshold, the dispatcher falls back to the static table even with the env var on. So the first few hundred tool calls per slot are functionally identical to the default.
2. **The reward signal is unproven on every real-world workload.** Our convergence proptest passes 30 seeds × 1000 simulated calls at ≥ 95% optimal-arm allocation. Real agents are messier. We want a few weeks of opt-in usage with the dashboard's Adaptive Modes panel watching convergence before flipping it on broadly.
3. **The downside is bounded but real.** Worst case on a bad slot is "~10% more tokens than the static table for that specific (language, file size, agent pattern) combination, until the bandit observes the re-reads and self-corrects within ~10-20 calls." The graph-layer wins (`find_code`, `get_symbol_context`, etc.) are independent and untouched — you never pay more than no-Memtrace.

If you want to try preview mode:

```bash
# Enable the adaptive bandit for this shell session
export MEMTRACE_ADAPTIVE_MODES=1
memtrace start

# Or per-process
MEMTRACE_ADAPTIVE_MODES=1 memtrace mcp
```

To force a specific default mode regardless of the bandit:

```bash
export MEMTRACE_DEFAULT_MODE=map  # or raw / lightweight / aggressive
```

`MEMTRACE_DEFAULT_MODE` always wins over `MEMTRACE_ADAPTIVE_MODES` and over the static table. Useful for reproducible benchmarks or for forcing a specific compression policy in CI.

You'll see slot activity light up in the dashboard's **Adaptive Modes** panel as the bandit collects samples. Once any slot crosses 50 samples, that slot starts using the bandit's choice instead of the static fallback. Bad slots are visible — operators can spot a converged-on-a-bad-arm slot and override with `MEMTRACE_DEFAULT_MODE` or report it for the heuristic to be tuned.

---

## What this means for token cost

**Before LeanCTX Native:** an agent reading a 12 KB Rust function got 12 KB of raw bytes — including doc comments, imports, blank lines.

**After LeanCTX Native (mode=map):** the same call returns ~1.2 KB of signatures only. The agent then either has enough to answer the user's question, or makes a follow-up call with `mode=lightweight` or `mode=raw` for the specific section it needs.

End-to-end on a typical session, internal benchmarks show **84.7% token reduction vs raw reads** on the file-content layer. Stacked on top of Memtrace's existing graph-query wins (which avoid file reads entirely whenever the answer is structural), the combined surface is **the most token-efficient code-intelligence layer publicly available** as of this writing.

---

## Compatibility

| Surface | Backwards-compat | Notes |
|---|---|---|
| `get_source_window` no `mode` arg | ✅ unchanged | Default `raw`, byte-identical response shape |
| `get_source_window` with `mode` | ➕ new field | Add `original_bytes`, `compressed_bytes`, `ratio`, `mode`, `_meta` |
| `get_directory_tree` | ➕ new tool | Tool didn't exist pre-v0.3.53 |
| Token-savings ledger | ➕ new endpoints | `GET /api/value/session/:id`, `/api/value/aggregate`, `/api/value/config` |
| Every other MCP tool | ✅ unchanged | Server-side instrumentation only; no response shape changes |

No agent has to opt in to compression. Agents that ignore the `mode` parameter get the static-table default (or the bandit's pick if you're in preview mode), and the response shape stays compatible with pre-v0.3.51 callers.

---

## Roadmap

- **v0.3.58+ (planned):** Default-on for the bandit once we've watched a month of opt-in convergence in production. The static table will remain as the under-50-samples floor.
- **Tunable reward window:** The current heuristic counts a "wider re-call within 5 turns" as negative reward. We'll expose this as `MEMTRACE_BANDIT_WINDOW_TURNS` once we've seen enough real-world data to know whether 5 is the right default.
- **Per-tool ledger formulas:** The byte-savings calculation per tool currently uses heuristics (`get_codebase_briefing` → estimated raw graph dump size minus rendered briefing). We'll publish the formula table so integrators can audit + dispute specific numbers.

---

## Appendix — env vars

| Variable | Purpose | Default |
|---|---|---|
| `MEMTRACE_ADAPTIVE_MODES` | Enable Thompson-sampling bandit for default-mode selection | `0` (off — preview mode opt-in) |
| `MEMTRACE_DEFAULT_MODE` | Force a default mode (`raw` / `lightweight` / `aggressive` / `map`) — overrides bandit + static | unset |
| `MEMTRACE_LEDGER_RETENTION_DAYS` | Vacuum ledger entries older than N days | `30` |

---

**Released:** v0.3.52 (2026-05-05) → v0.3.57 (2026-05-06)
**Round:** [`leanctx-phase-{1,2,3,4}-*`](https://github.com/syncable-dev/memtrace/tree/main/docs/superpowers) (private repo — round artifacts)
**Bench JSON:** `docs/perf-history/2026-05-{05,06}-leanctx-phase-{1,4}.json`

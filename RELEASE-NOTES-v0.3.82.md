# Memtrace v0.3.82 — Field-report fixes round

Big set of fixes landing, most flagged by real operators in the field. Credits in each section.

## TL;DR

| Bug / feature | Was | Is |
|---|---|---|
| Embed daemon hung silently on memory-pressured hosts | exit 0 with no banner | exit 75 with `BreakerReason: PressureCritical`, marker not stamped on zero-success |
| Codex thrashed on `get_symbol_context` for unknown symbols | hard `Err` → 10-min inactivity timeout | `Ok({found: false, _note: "fall back to filesystem"})` |
| `--workspace` daemon missed `git commit`s | only reacted to source-file saves | watches `.git/refs/heads/<branch>` too, picks up commits within 5s, debounced |
| Windows: 21-repo re-index every restart | drive-letter case + `\\?\` prefix made `repo_id` unstable across sessions | `RepoIdentity::from_path` normalizes; restart seeds in <200ms per repo |
| No background daemon mode | foreground only, terminal must stay open | `memtrace daemon install` writes launchd plist / systemd unit / Windows Service |
| Pre-commit hook 2 min in agentic pipelines | sync, blocking, auto-installed | **opt-in**, plus `--agent-mode` (15ms detached fire-and-forget) + 4 OOM guards |
| UserPromptSubmit hook fired on every message | dozens per prompt in automated runs | per-session 2-min debounce via lock file |
| LeanCTX value ledger flat at zero | default `mode` was `Raw` (no compression) | default `Lightweight`, every call now contributes `_meta.context_avoided_bytes > 0` |

## Breaking changes

⚠️ **Pre-commit hook is now opt-in, not auto-installed.**

If you had it installed from a prior version, it's untouched. Bare `memtrace install` no longer wires it up. To get it back: `memtrace install-hooks --pre-commit`. To remove it cleanly: `memtrace uninstall-hooks`.

This was driven by feedback from operators running automated agentic pipelines (100+ prompts/day), where the synchronous pre-commit hook was burning 8 minutes of session time on a 4-commit prompt.

## What changed by surface

### Embedding pipeline — the silent crash on memory pressure (h/t @Corpo)

**The bug:** on macOS hosts under memory pressure (typically Apple Silicon with 16-36 GB RAM running multiple agents), the JINA-fp32 embedding worker would stall during inference. The 60s batch timeout fired, the worker was respawned, and within ~13 seconds the OS jetsam'd the process. The npm shim silently propagated this as exit 0 — operators saw "Memtrace just disappeared". You'd then have to set `MEMTRACE_SKIP_EMBED=1 + MEMTRACE_NO_REPLAY=1` to keep things stable.

**The fix:**
- New system-pressure probe (Mach `host_statistics64` on macOS, `/proc/meminfo` on Linux, `GlobalMemoryStatusEx` on Windows)
- Embedding loop now consults the probe before each batch
- Sustained `Critical` pressure for 60s consecutive trips a circuit breaker
- Daemon exits with code 75 (`EX_TEMPFAIL`) and a clear banner: `"BreakerReason: PressureCritical"`
- The "embed complete" marker is no longer stamped when zero embeddings actually succeeded
- Next run won't skip thinking it succeeded

**New env knobs:**
- `MEMTRACE_EMBED_PRESSURE=off|normal|warn|critical` (default `warn`) — gate threshold
- `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS=N` (default `60`) — bump on slow CPU paths

**New startup banner** shows the gates so you can see what's enforced:
```
RSS ceiling: 10 GB                  (override: MEMTRACE_EMBED_RSS_LIMIT_GB)
Pressure gate: warn                 (override: MEMTRACE_EMBED_PRESSURE)
Batch: 64 inference / 128 write     (override: MEMTRACE_EMBED_BATCH_SIZE / MEMTRACE_EMBED_WRITE_BATCH)
CoreML EP: enabled                  (disable: MEMTRACE_DISABLE_COREML=1)
Breaker: Closed                     (reset: kill -USR1 <pid> | mcp embed.reset_breaker)
```

You can drop `MEMTRACE_NO_REPLAY=1` workarounds — the underlying hang is gone.

### Search tools — Codex thrashing on not-found (h/t @badmrpotatohead)

**The bug:** `get_symbol_context` and 4 sibling tools threw hard errors when called on a symbol not in the indexed graph. Codex (and other agents) would retry the same call instead of falling back to filesystem search. With Claude Code's 10-min inactivity timeout, sessions died.

**The fix:** 5 tools converted from `Err("not found")` to `Ok({found: false, _note: "..."})`:
- `get_symbol_context`
- `get_impact`
- `analyze_relationships`
- `get_episode_replay`
- `find_code`, `find_symbol`, `get_timeline`, `find_dependency_path` were already correct — added `found` + `_note` for uniformity

The `_note` field is textual: `"Symbol not found in indexed graph. Falling back to filesystem search is recommended."` — so agents have a clear next-step signal.

`Err` is preserved for genuine input errors (invalid `query_type`, missing required params, etc.).

10 regression tests pinned so this can't drift back.

### Workspace daemon — auto-watch for git commits (h/t @Magalz + @badmrpotatohead)

**The bug:** `memtrace start --workspace .` reacted to source-file saves but missed `git commit`s that didn't touch additional source files. Operators had to re-run `memtrace start` to pick up the commit, or wait until the next file save in the worktree. In sessions where Codex committed and immediately queried for new symbols, the daemon returned "not found" because the commit hadn't been indexed yet.

**The fix:** the workspace daemon now watches `.git/refs/heads/<active_branch>` in addition to source files. Fresh commits are picked up within 5s, debounced (interactive rebase = many ref updates collapse to one delta sync after settling).

**New env knobs:**
- `MEMTRACE_GIT_REF_WATCH=on|off` (default `on` for `--workspace`, `off` otherwise)
- `MEMTRACE_GIT_REF_WATCH_DEBOUNCE_MS=N` (default `2000`)

Branch switch (`git checkout other-branch`) re-targets the watcher to the new active ref without restart. Detached-HEAD doesn't crash — logs once and falls back to file-save trigger.

### Windows restart persistence (h/t @badmrpotatohead)

**The bug:** Sivant's setup at `C:\Sivants Projects\.memdb` re-indexed all 21 repos from scratch on every `memtrace start`. Within a session the fast-path worked (skip in <200ms when no drift). Across restarts it didn't.

**Root cause:** 4 sites in the bin derived `repo_id` via `repo_root.file_name().to_string_lossy().to_string()`. On Windows, the same on-disk path produces different basename strings across sessions:
- Drive-letter case drift (`c:` vs `C:`)
- Trailing-backslash drift from tab-completion
- Win32 verbatim namespace prefix `\\?\` (entered by `std::fs::canonicalize` for long paths)
- Mixed `/` vs `\` separators

The recognition gate (`count_records(repo_id)`) keys directly off this string. So MemDB held thousands of nodes for a repo, but `count_records` returned 0 under the alternate-spelling key — full re-index path.

**The fix:** every `repo_id` derivation now flows through `RepoIdentity::from_path`:
- drive-letter case → uppercase
- `\\?\` prefix stripped
- `/` + `\` collapsed
- trailing separators trimmed
- `.`/`..` resolved

Pure component-string operation — no filesystem touch (works on cold-restart codepath where the path may have moved).

**Schema marker** at `<data_dir>/.memtrace-schema.json` with a clear banner on mismatch (`"Schema version mismatch (found vN, expected vM) — full re-index required"`). Set `MEMTRACE_LOG_SCHEMA_DETECTION=1` for verbose detection tracing.

Sivant's 21 repos should seed in <200ms each on restart now.

### Background daemon mode (h/t @badmrpotatohead)

**The ask:** "Do we have to keep the PowerShell window open the whole time?"

**The fix:** new `memtrace daemon` subcommand:
- `memtrace daemon install` — writes launchd plist (macOS), systemd user-unit (Linux), or Windows Service (`sc.exe`)
- `memtrace daemon uninstall`
- `memtrace daemon start`
- `memtrace daemon stop`
- `memtrace daemon status` — JSON output: `{running, pid, uptime_secs, platform}`

Daemon survives terminal close, user logout (where supported), system reboot (auto-restart). Same MCP tools work — no client config changes.

### Pre-commit hook (h/t @badmrpotatohead — Orbit operator)

**The ask:** "The pre-commit hook takes ~2 minutes on my machine. Claude Code has a 10-minute inactivity timeout. A prompt that commits 4 times burns 8 minutes just on hooks."

**Three fixes:**

**1. Hook is now opt-in.** Bare `memtrace install` no longer wires it up. Existing installs aren't touched. Re-run of `memtrace install` when hook is detected prints uninstall instructions.

**2. New `--agent-mode` flag** for `memtrace pre-commit` (or `MEMTRACE_PRECOMMIT_MODE=agent` env): forks the daemon-ping detached and returns 0 immediately. Wall-clock ~15ms warm-cache instead of ~2 minutes. The daemon eats the work async; Claude Code never sits there waiting.

**3. Four OOM guards** on the pre-commit path itself, all with env overrides. Set `0` to opt out:
- `MEMTRACE_PRECOMMIT_MAX_RSS_MB=512` — RLIMIT_AS via libc (Linux enforced; macOS is a documented no-op per kernel)
- `MEMTRACE_PRECOMMIT_MAX_DIFF_BYTES=1048576` — skip analysis on huge commits
- `MEMTRACE_PRECOMMIT_MAX_SYMBOLS=500` — cap rendered warnings
- `MEMTRACE_MEMDB_LOOPBACK_PORT=50051` — 50ms TCP probe → reuse running daemon instead of spawning fresh in-process MemDB

Existing escape hatches still work: `MEMTRACE_PRECOMMIT=off`, `git commit --no-verify`, `memtrace uninstall-hooks`.

### UserPromptSubmit hook debounce (h/t @badmrpotatohead — Orbit)

**The ask:** "The UserPromptSubmit hook fires on every message Claude receives during a session. In an automated run that's dozens of fires per prompt."

**The fix:** per-session-id debounce via lock file at `~/.memtrace/hook-debounce/<session_id>.lock`. After the hook fires, suppresses further fires within `MEMTRACE_HOOK_DEBOUNCE_SECS` (default 120 = 2 min) for the same session.

Session-ID priority:
1. `CLAUDE_SESSION_ID` env (if Claude Code sets it)
2. `CLAUDE_CONVERSATION_ID` env
3. Fallback: SHA-1 of `PPID + parent process start-time`

**New env knobs:**
- `MEMTRACE_HOOK_DEBOUNCE_SECS=N` (default `120`; set `0` to disable debounce)
- `MEMTRACE_HOOK_MODE=off` (already existed) — disables hook entirely
- `MEMTRACE_HOOK_ORPHAN_CLEANUP_MAX=32` (advanced) — bound for orphan-lock cleanup

100-message session now triggers the daemon at most ⌈100 × turn_avg / debounce_window⌉ times instead of 100.

### Operator diagnostics

**New MCP tools:**
- `embed.diag` — returns `{pressure, breaker_state, rss_mb, host_profile, last_phase2_per_repo}`
- `embed.reset_breaker` — resets the circuit breaker without daemon restart, audit-logged via `tracing::info!` at target `memtrace::embed::breaker`

**`memtrace status --json`** now includes `phase2.per_repo.<repo>.{last_result, last_at, successful, total, reason?}` for CI consumers.

### LeanCTX file compression (the value ledger fix)

**The bug:** value ledger showed zero `bytes_avoided` from `get_source_window` calls. Default `mode` was `Raw` (no compression) — unless agents explicitly passed `mode: "aggressive"` or operators set `MEMTRACE_ADAPTIVE_MODES=1`, every call returned verbatim bytes → ledger flat at zero. The bandit was correctly implemented but starving on cold-start (no rewards = no convergence = no compression = no rewards — chicken/egg).

**The fix:** default mode flipped from `Raw` → `Lightweight` (whitespace + blank-line cleanup, ~10-30 % reduction, zero semantic loss). Every call now contributes non-zero `_meta.context_avoided_bytes`. Ledger shows compression. The bandit's reward feedback loop also unblocks.

If you want more aggressive compression: agents can pass `mode: "aggressive"` (~70-95 % reduction depending on language) or `mode: "map"` (function signatures only, ~95-99 %).

### Hybrid retrieval — natural-language → log-line queries

**The bug:** queries like `"embedding writer idle watchdog warn 30 seconds"` didn't return the function containing that log line. The JINA-768d embedding diluted single salient lines inside long (~250-line) function bodies. Pre-rewrite this would have hit on BM25 substring; post-rewrite the long-function-as-blob embedding lost the signal.

**Two-part fix:**

**1. Long-function chunking:** functions whose body exceeds `MEMTRACE_LONGFN_CHUNK_THRESHOLD` lines (default 80) are embedded as overlapping sub-spans (default 60-line chunks, 20-line overlap). Each sub-span carries a synthetic rid `{parent_rid}#chunk_{i}`. At search time, results dedup to the parent symbol; rank uses the best chunk score.

**2. Log-string indexing:** function-body string literals are extracted at index time and added to a new `body_strings` BM25 field with a 0.5× boost. Substring matches in long-function bodies now surface the parent symbol via the BM25 leg.

**New env knobs (mostly internal — defaults are reasonable):**
- `MEMTRACE_LONGFN_CHUNK_THRESHOLD=80`
- `MEMTRACE_LONGFN_CHUNK_SIZE=60`
- `MEMTRACE_LONGFN_CHUNK_OVERLAP=20`
- `MEMTRACE_FIELD_BOOST_BODY_STRINGS=0.5`

## Test fortress

345 named regression tests landed across two rounds:
- 220 in `fortress-round-1` (embed pressure, lifecycle, hybrid retrieval, operator diagnostics, pre-push gate)
- 125 in `fortress-round-2` (workspace ref-watcher, Windows persistence, daemon mode, hook debounce)

All deterministic 3× by construction (no wall-clock sleep, injected probes/clocks/fakes everywhere, checked-in proptest seeds, every test has an `// invariant: …` doc comment).

A new `memtrace install-hooks --pre-push` installs a managed pre-push git hook that runs the full fortress matrix before any `git push`, with retry-3 flake detection. Bypass: `git push --no-verify`, `MEMTRACE_PREPUSH=off`.

## Field reporters — credits

- **@Corpo** — flagged the silent embed crash on Apple Silicon (Hermesdeploy). The repro paragraph in your screenshot pointed straight at it.
- **@badmrpotatohead** — flagged 5 of the 9 surfaces fixed this round: Codex `not_found` thrash, workspace `git commit` not picked up, Windows 21-repo re-index, no background daemon mode, and both Orbit-pipeline hook problems (pre-commit 2-min latency + UserPromptSubmit fire-per-message). Sustained, specific, repro-shaped reports across multiple sessions.
- **@Magalz** — flagged the workspace daemon not auto-watching `git commit`s, plus the `symbol_exists` probe ask (which we resolved by making existing tools return empty Ok rather than adding a redundant call).

If you hit something not on this list, drop a message — most of these were in the field for days before getting flagged. Quicker reports = quicker fixes.

---

## v0.3.83 — Apple Silicon int8 default

**The bug:** v0.3.82 + the f0fcf221 model swap (bge-small → JINA-code) defaulted Apple Silicon Heavy hosts (M-Max, M-Ultra, ≥24 GB) to `fp32` quant. fp32 JINA-code falls back to the slow CoreML CPU path — the Apple Neural Engine only accelerates `int8` graphs. Field operators on Heavy-tier Apple Silicon hosts reported a ~614 MB resident model + 10-20× slower embed pass (or full hangs) versus the pre-f0fcf221 era. Standard / Light tiers were unaffected (already `int8`).

**The fix:** the auto-pick now returns `int8` whenever the host's CPU brand parses as Apple Silicon (`M1` / `M2` / `M3` / `M4` Base / Pro / Max / Ultra), regardless of tier. The tier still drives batch sizes, thread caps, and RSS ceilings — only the quant default flips. Workstation Linux / Windows hosts on Heavy tier are unchanged: CUDA / DirectML still accelerates `fp32` there, and the `fp32` default stays.

**Opt-back:** operators who want `fp32` on Apple Silicon (for a benchmark or a throughput-bound workload) set `MEMTRACE_EMBED_QUANT=fp32`. The env override wins over the auto-pick in both directions, and is also surfaced in the runtime-gates banner so the active value is always visible:

```text
EmbedQuant: int8                    (override: MEMTRACE_EMBED_QUANT=fp32)
```

(banner is now 6 lines; was 5 in v0.3.82 — added the `EmbedQuant:` line so operators can read the active quant + the env var that flips it without grepping the docs.)

**Scope summary:**
- Apple Silicon (any tier): default flipped `fp32` → `int8`
- Linux / Windows Heavy tier with discrete GPU or DirectML: unchanged (`fp32`)
- Linux / Windows Standard / Light tier: unchanged (`int8`)
- `MEMTRACE_EMBED_QUANT` env override: unchanged (still wins, both directions)

**Field reporters — credits:**
- Apple Silicon Heavy operators flagged the ~614 MB resident + 10-20× slowdown via the runtime banner — the `embed=fp32` value paired with the slow CoreML compile pointed straight at the regression.

### ONNX Runtime startup probe (the real root cause of the silent hangs)

The benchmark validating the int8 default exposed the actual cause of the helix-db crash class: ort 2.0.0-rc.11's `setup_api` panics deep inside a worker thread when `libonnxruntime.dylib` isn't where it expects. The panic was being swallowed by the supervisor (silent hang) OR percolating up after a long delay (silent exit-0).

Now `memtrace start` calls `probe_ort_runtime()` BEFORE Phase 2 — fast, microseconds. If the dylib can't load, daemon exits 75 immediately with `BreakerReason: OrtUnavailable` and a clear diagnostic showing platform-specific install instructions plus the `MEMTRACE_SKIP_EMBED=1` workaround.

Affects all Apple Silicon hosts where `libonnxruntime.dylib` isn't system-installed (most homebrew-less setups). Workarounds:

- `brew install onnxruntime` (recommended)
- `MEMTRACE_SKIP_EMBED=1 memtrace start` (structural graph only — embedding stage skipped, semantic search disabled, structural graph + symbol search still work)
- `MEMTRACE_NO_REPLAY=1` (also skips git replay)

The probe is idempotent and skipped entirely when `MEMTRACE_SKIP_EMBED=1` is already set, so structural-only operators on hosts without the dylib don't see a startup gate.

Exit code 75 is `EX_TEMPFAIL` from `sysexits.h` — the "try again after fixing the host" code, distinct from 78 (`EX_CONFIG`, used by the AVX2 baseline check on x86_64).

---

## v0.3.84 — First-run-aware embed warmup

**The bug:** with the v0.3.82 JINA-code migration AND the v0.3.83 int8 default flip in place, the very first `model.embed()` call on Apple Silicon triggers a CoreML graph compile for the ANE that takes 60–300 s the first time the graph is seen for a given model. The `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` default (60 s) was sized for warm caches; the cold-start spike exceeded it on most M1/M2 8 GB hosts, the embed circuit breaker tripped on `TimeoutAfter { secs: 60 }`, and the daemon died before a single batch completed. Subsequent runs are 5–15 s because both `~/.memtrace/fastembed_cache/` (the .onnx file) and `~/Library/Caches/com.apple.coremlcompiler/` (the compiled ANE bytecode) are warm — so the bug was first-run-only, but it was a hard wall every operator hit.

**The fix:** three pieces, all opt-out-able.

### 1. Process-local two-tier batch timeout

`MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` (60 s default) is now used only for the *steady-state* path. The first batch of a process gets a separate, longer window driven by a new env var:

| Env var | Default | When it applies |
|---|---|---|
| `MEMTRACE_EMBED_FIRST_BATCH_TIMEOUT_SECS` | `600` (10 min) | Until any embed batch completes successfully in this process |
| `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` | `60` | Every batch after the first one completes |

Detection is process-local — a single `AtomicBool` flips false → true when the writer task receives the first `EmbedJob::Batch`. Daemon restart resets it (which matches reality: macOS may evict the CoreML compiler cache between runs, rare but documented). On Linux / Windows the cold-start cost is much smaller; the wider first-batch window is harmless there because once warm the steady-state default is identical to the pre-v0.3.84 behaviour.

### 2. `memtrace warmup` subcommand

```bash
memtrace warmup
memtrace warmup --model bge-small  # opt-out for non-default model
```

Loads the active embedding model (downloading via fastembed if missing), runs ONE dummy `embed("warmup")` call to force the CoreML graph compile, and reports timing + cache locations:

```text
  memtrace warmup — pre-compile embedding-model graph

  ✓  ONNX Runtime: ready
  ◆  Model: jina-embeddings-v2-base-code/int8

  ⏳  Loading + compiling graph (may take 60-300s on cold Apple Silicon)...
  ✓  Warmup complete in 2.2s  (model dim = 768)

  Caches:
    fastembed model    : ~/.memtrace/fastembed_cache (612 MB on disk)
    per-repo embeds    : ~/.memtrace/embed-cache
    CoreML compiled    : ~/Library/Caches/com.apple.coremlcompiler/ (managed by macOS)
```

Exits 0 on success, 1 with a structured diagnostic on failure (same surface shape as `cmd_start`'s ort probe gate). Recommended use: run once after `npm install -g memtrace` so the first `memtrace start` finds everything warm.

### 3. First-run UX line

When the embed phase detects "this process has not yet completed a batch AND the per-repo embed-cache redb has zero entries for the active model id", it emits a one-shot status line on stderr:

```text
  ⏳  First-run: warming embedding model graph for Apple Silicon ANE (one-time, ~60-180s)...
```

After the first batch completes, a follow-up line:

```text
  ✓  Model warm — subsequent starts will be fast
```

Purely informational — sets expectations so an operator doesn't kill a daemon that's mid-graph-compile.

**Scope summary:**

- All Apple Silicon hosts: first-batch timeout silently lifted to 600 s on cold start
- Linux / Windows hosts: identical defaults to v0.3.83 in steady state; first-batch window is wider but the cold-start cost is small enough that nothing changes in practice
- `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS` semantics: unchanged for the warm path
- New: `MEMTRACE_EMBED_FIRST_BATCH_TIMEOUT_SECS` (override the 600 s cold-start cap)
- New: `memtrace warmup [--model <name>]` (one-shot pre-compile)

**Field reporters — credits:**
- Apple Silicon operators on the v0.3.83 int8 default flip flagged the cold-start breaker trip immediately after upgrading. The repro shape ("daemon dies on first index, runs fine on every subsequent start after the cache warms") was diagnostic.


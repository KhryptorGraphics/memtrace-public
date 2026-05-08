# Memtrace v0.3.85 — Apple Silicon embed fix + retrieval cleanup

Rolled-up release covering v0.3.82–v0.3.85 (the dev-only versions never reached
npm). Field-report fixes from v0.3.82 are still included; v0.3.83/84/85 add
the Apple Silicon memory fix and a retrieval-noise cleanup.

Real reports drove this. Crediting up front:
**@Corpo @badmrpotatohead @Magalz** — sustained, specific, repro-shaped
reports across multiple sessions.

## TL;DR

| Bug / feature | Was | Is |
|---|---|---|
| **`memtrace start` jetsam-killed during embed on commodity Macs (16/24/36 GB)** | jina-code on Apple Silicon spiked to 60+ GB during CoreML graph compile | static-link `ort` on `macos+aarch64` → 5–7 GB peak, completes in 16 min |
| **`body_strings` BM25 noise** | wave-3 added a 0.5× BM25 field that indexed every string literal in every function body — added log-line "matches" to wrong files, hurt ranking | removed; quality unchanged within noise |
| Embed daemon hung silently on memory-pressured hosts | exit 0 with no banner | exit 75 with `BreakerReason: PressureCritical`, marker not stamped on zero-success |
| Codex thrashed on `get_symbol_context` for unknown symbols | hard `Err` → 10-min inactivity timeout | `Ok({found: false, _note: "fall back to filesystem"})` |
| `--workspace` daemon missed `git commit`s | only reacted to source-file saves | watches `.git/refs/heads/<branch>` too, picks up commits within 5s, debounced |
| Windows: 21-repo re-index every restart | drive-letter case + `\\?\` prefix made `repo_id` unstable across sessions | `RepoIdentity::from_path` normalizes; restart seeds in <200ms per repo |
| No background daemon mode | foreground only, terminal must stay open | `memtrace daemon install` writes launchd plist / systemd unit / Windows Service |
| Pre-commit hook 2 min in agentic pipelines | sync, blocking, auto-installed | **opt-in**, `--agent-mode` (15ms detached) + 4 OOM guards |
| UserPromptSubmit hook fired on every message | dozens per prompt in automated runs | per-session 2-min debounce via lock file |
| LeanCTX value ledger flat at zero | default `mode` was `Raw` (no compression) | default `Lightweight`, every call now contributes `_meta.context_avoided_bytes > 0` |

## Breaking changes

⚠️ **Pre-commit hook is now opt-in, not auto-installed.**

Untouched if you already had it from a prior version. New installs no longer wire it up. Restore: `memtrace install-hooks --pre-commit`. Remove: `memtrace uninstall-hooks`.

Driven by reports of automated agentic pipelines burning 8 minutes of session time on a single 4-commit prompt. Even when installed, see the env knobs below — the hook is now survivable.

---

## v0.3.85 — Apple Silicon Phase-2 fix (h/t @Corpo for the original embed-hang report)

**Root cause:** `release(0.3.62)` added the `load-dynamic` feature to the `ort` crate so x86 hosts could swap AVX2 vs noavx2 dylibs at runtime (Hermesdeploy/Corpo's noavx2 fix). On Apple Silicon there's no AVX2/noavx2 split — the dlopen'd `libonnxruntime.dylib` triggers a CoreML-EP graph-compile blowup that the statically-linked ORT does not.

Bisected v0.3.55 → HEAD across 86 commits to confirm `release(0.3.62)` as the breaking commit.

**Fix:** target-cfg ort feature in `Cargo.toml`:
- `macos + aarch64` → `features = ["std", "ndarray"]` (static)
- everyone else → `features = ["std", "ndarray", "load-dynamic"]` (dynamic, preserves Corpo's noavx2 path)

**Measured on 36 GB M3 Max indexing Memrack/Memtrace:**

| | Phase 1 | Phase 2 peak RSS | Compressed | Outcome |
|---|---|---|---|---|
| v0.3.55 (static) | 2.18 sec | 7 GB | 0–153 MB | ✓ |
| v0.3.84 (dynamic) | 150 sec | 60+ GB | 50+ GB | ✗ jetsam |
| **v0.3.85 (target-cfg static)** | **150 sec** | **7 GB** | **0 MB** | ✓ **6117/6117 in 16 min** |

Linux x86 / Windows / Intel Mac unchanged — they keep `load-dynamic`, Corpo's noavx2 detection still works.

## v0.3.85 — `body_strings` BM25 stamping removed

Wave-3 added a 0.5×-boosted BM25 field that indexed string literals inside function bodies. Stated goal was NL→log-line search. In practice it polluted the rerank top-30 (queries like `Embedding` matched log lines in unrelated functions, displacing the correct file).

**A/B on mempalace 1k × 3 variants (jina-code + rerank ON):**

| | acc@1 | MRR | latency |
|---|---|---|---|
| WITH body_strings | 93.27% | 0.9575 | 894 ms |
| WITHOUT body_strings | **93.57%** | **0.9597** | 916 ms |

+0.30pp acc@1 from removal, latency unchanged within noise. The feature wasn't earning its keep.

## v0.3.85 — Phase-1 leak fix

Wave-3 added `body_strings: Vec<BodyString>` as a field on `FileParseResult`. The struct is held in a `HashMap<PathBuf, FileParseResult>` for the resolver pass — every parsed file's literal payload sat in memory simultaneously across the whole repo. On Memrack workspace indexing this added ~4 GB to Phase 1 RSS, jetsam-killing the daemon at the 4th repo (helix-db).

Field removed; inline string-literal extraction was already disabled by the BM25 stamping removal above.

---

## Field-report fixes (originally landed in v0.3.82)

### Embed daemon hung silently on memory-pressured hosts (h/t @Corpo)
- Was: `exit 0` with no banner; you'd find out 30 min later that nothing indexed
- Is: pressure gate before each batch. Sustained Critical (60 s) trips a circuit breaker, daemon exits 75 with `BreakerReason: PressureCritical`. Marker no longer stamped on zero-success runs
- Knobs: `MEMTRACE_EMBED_PRESSURE=off|normal|warn|critical` (default `warn`), `MEMTRACE_EMBED_BATCH_TIMEOUT_SECS=N` (default 60)
- New startup banner shows the gates so you can see what's enforced
- You can drop `MEMTRACE_NO_REPLAY=1` — underlying hang is gone

### Codex thrashed on `get_symbol_context` for unknown symbols (h/t @badmrpotatohead)
- Was: hard `Err` → Codex retries forever → 10-min Claude inactivity timeout
- Is: 5 tools (`get_symbol_context`, `get_impact`, `analyze_relationships`, `get_episode_replay`, plus uniformity on `find_code` / `find_symbol` / `get_timeline` / `find_dependency_path`) return `Ok({found: false, _note: "Symbol not found in indexed graph. Falling back to filesystem search is recommended."})`
- Pinned by 10 regression tests so it can't drift back

### Workspace daemon auto-watches git commits (h/t @Magalz + @badmrpotatohead)
- Was: `--workspace` daemon only reacted to source-file saves; a commit that didn't touch additional files between commits was missed
- Is: watches `.git/refs/heads/<branch>` too. Commits picked up within 5 s
- Knobs: `MEMTRACE_GIT_REF_WATCH=on|off` (default on for `--workspace`), `MEMTRACE_GIT_REF_WATCH_DEBOUNCE_MS` (default 2000)

### Windows: 21-repo re-index every restart (h/t @badmrpotatohead — Sivant repro)
- Was: drive-letter case drift (`c:` vs `C:`), `\\?\` prefix from canonicalize, and mixed `/` vs `\` separators produced different `repo_id` strings across sessions; `count_records(repo_id)` returned 0 → full re-index
- Is: every `repo_id` derivation flows through `RepoIdentity::from_path` which normalizes platform-specific path quirks; 21 repos seed in <200 ms each on restart
- Bonus: schema marker at `<data_dir>/.memtrace-schema.json` with banner on mismatch

### Background daemon mode — no terminal needed (h/t @badmrpotatohead)
New `memtrace daemon` subcommand:

```
memtrace daemon install        # launchd / systemd / Windows Service
memtrace daemon status         # JSON: {running, pid, uptime_secs, platform}
memtrace daemon stop|start|uninstall
```

Survives terminal close, user logout, system reboot.

### Pre-commit hook 2-min latency in agentic pipelines (h/t @badmrpotatohead — Orbit)
Hook is opt-in (see breaking change above). When installed:

```
MEMTRACE_PRECOMMIT_MODE=agent          # or: memtrace pre-commit --agent-mode
                                       # forks daemon-ping detached → exits in ~15ms
```

Plus 4 OOM guards (set 0 to opt out):

```
MEMTRACE_PRECOMMIT_MAX_RSS_MB=512      # RLIMIT_AS (Linux enforced)
MEMTRACE_PRECOMMIT_MAX_DIFF_BYTES=1MB  # skip huge commits
MEMTRACE_PRECOMMIT_MAX_SYMBOLS=500     # cap rendered warnings
MEMTRACE_MEMDB_LOOPBACK_PORT=50051     # daemon-reuse via TCP probe
```

### UserPromptSubmit hook fired on every message (h/t @badmrpotatohead — Orbit)
- Was: dozens of fires per prompt in automated runs, daemon health-probe spammed
- Is: per-session 2-min debounce via lock file at `~/.memtrace/hook-debounce/`
- Knobs: `MEMTRACE_HOOK_DEBOUNCE_SECS=120` (default; 0 = disable debounce), `MEMTRACE_HOOK_MODE=off` (existed already, kills hook entirely)

### Operator surface
- `embed.diag` MCP tool — `{pressure, breaker_state, rss_mb, host_profile, last_phase2_per_repo}`
- `embed.reset_breaker` MCP tool — reset without daemon restart, audit-logged
- `memtrace status --json` — `phase2.per_repo.<repo>.{last_result, last_at, successful, total}`

### LeanCTX value ledger now actually shows compression
Default `mode` for `get_source_window` flipped `Raw` → `Lightweight`. Every call contributes non-zero `_meta.context_avoided_bytes`. Bandit reward loop unblocks. Want more aggression? Pass `mode: "aggressive"` (~70-95%) or `mode: "map"` (~95-99%).

### Test fortress
345 named regression tests added across two rounds, all deterministic 3×. New `memtrace install-hooks --pre-push` installs a managed git pre-push hook that runs the full matrix with retry-3 flake detection before any `git push`.

Bypass: `git push --no-verify` OR `MEMTRACE_PREPUSH=off`.

## Verified end-to-end

Local install (`/Users/alexthh/.npm-global/lib/node_modules/memtrace/...`) replaced; `memtrace --version` reports `0.3.85`. Indexing the Memrack 6-repo workspace (jina-code + 768d, default settings) completes Phase 1 + Phase 2 cleanly with steady-state ~7 GB RSS, zero compression, zero swap pressure.

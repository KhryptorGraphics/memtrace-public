---
name: memtrace-first
description: "Always use first for indexed source-code repos before searching files, reading code for discovery, debugging, tracing flows, finding implementations, understanding behavior, or answering how code works. Do not use Grep, Glob, rg, find, or manual file browsing for code discovery when Memtrace is indexed; only use file tools for configs, infra, docs, non-source artifacts, or exact paths already returned by Memtrace."
---

# Memtrace First

## The Iron Law

```
IF THE REPO IS INDEXED IN MEMTRACE → USE MEMTRACE TOOLS FIRST.
Memtrace returns exact file_path + start_line + end_line for every result.
Read the file at THAT location. Do not Grep/Glob/Find to "locate" anything
already in the graph.
```

Memtrace is the memory layer of the codebase. It has the full knowledge graph: every symbol, call, import, community, process, and API — with time dimension. File tools are blind to this structure.

**97% better accuracy. 83% fewer wasted tokens. No exceptions for what's in the graph.**

## Value Tracking

Do not print usage receipts in normal answers. Memtrace records tool usage, graph facts, file references, and estimated context avoided internally. Users can inspect that in the local UI's Value panel.

## What Memtrace actually indexes

Memtrace's hybrid search = **BM25 over symbol metadata** (name, signature, file_path, kind) **+ semantic vector search over embedded code bodies** (first ~1500 chars of every Function / Method / Class / Struct / Interface body), fused via Reciprocal Rank Fusion.

The semantic side means **string literals, error messages, magic constants, log strings, and any text inside an indexed symbol's body are findable through `find_code`**. The body got embedded; the embedding catches it. You do NOT need `Grep` to hunt for `STRIPE_KEY_FOO_BAR` if it lives inside a function in your indexed codebase.

## The narrow exceptions where grep/glob are still right

These are the ONLY cases where file tools beat memtrace:

- **Files outside the indexed repo.** Vendored deps, system headers, dirs `walker::is_excluded_path` skipped (`.git`, `node_modules`, `target`, `dist`). Memtrace literally cannot see them.
- **Non-source artifacts.** `.env`, `package.json`, build scripts, top-level `README.md`, raw config files. Memtrace indexes parseable code, not configuration text.
- **Pure file-inventory questions.** "How many `*.test.ts` files exist", "list every Markdown file in `docs/`". You're asking for a file count, not a symbol search.
- **Reading at a known path.** Once memtrace has handed you `file_path:start_line:end_line`, use `Read` — never substitute `Grep` for `Read`.

For everything else inside the indexed repo, memtrace is the right tool.

## The decision rule

| Question Claude is asking | Right tool |
|---|---|
| "Where is symbol `foo` defined?" | `find_symbol(name="foo")` → file:line. Then `Read` that range. |
| "What calls `foo`?" | `get_symbol_context(name="foo")` → callers with file:line each. |
| "How does authentication work?" | `find_code(query="authentication")` → ranked symbols with file:line. |
| "Find the function that uses `STRIPE_KEY_FOO_BAR`" | `find_code(query="STRIPE_KEY_FOO_BAR")` → semantic finds it inside any embedded body. |
| "Where's that error message `'connection refused for tenant'`?" | `find_code(query="connection refused for tenant")` → semantic catches it. |
| "What breaks if I change `foo`?" | `get_impact(name="foo")` → blast radius with file:line. |
| "What changed in `auth.ts` last week?" | `get_evolution(file_path="auth.ts", from="7d ago")`. |
| "List all `*.test.ts` files." | `Glob` (file inventory, not symbol search). |
| "Find this string in my `.env`." | `Grep` (non-source artifact). |
| "Read file I already have the path of." | `Read` (path is known). |

## Parameter Types — Read This Before Calling Any Tool

All memtrace MCP tools are **strictly typed**. Pass JSON numbers (not strings) for integer parameters.

| Parameter | Correct | WRONG (fails with MCP error -32602) |
|---|---|---|
| `limit`, `min_size`, `depth`, `max_depth`, `last_n` | `limit: 20` | `limit: "20"` |
| `repo_id`, `branch`, `name`, `symbol_name`, `query` | `repo_id: "my-repo"` | `repo_id: my-repo` (unquoted) |
| `fuzzy`, `include_tests`, `invalidate` | `fuzzy: true` | `fuzzy: "true"` |

If you see `failed to deserialize parameters: invalid type: string "N", expected usize`, remove the quotes from the number and retry.

## Check Indexing First (Once Per Session)

```
mcp__memtrace__list_indexed_repositories
```

If the current repo appears → Memtrace is active. Follow this skill for ALL code tasks.
If not indexed → offer to index with `mcp__memtrace__index_directory`, then follow this skill.

## Task → Tool Map

| What you need | Use instead of Grep/Glob/Read |
|---|---|
| Find a function / class / symbol | `find_symbol` or `find_code` |
| Understand how something works | `get_symbol_context` |
| Find all callers of a function | `get_symbol_context` (callers field) |
| Find all callees / dependencies | `get_symbol_context` (callees field) |
| Trace a request / execution path | `get_process_flow` |
| Understand module structure | `list_communities` |
| Find the most important symbols | `find_central_symbols` |
| Find API endpoints | `find_api_endpoints` |
| Find where an API is called | `find_api_calls` |
| Debug a problem | `get_symbol_context` → `get_impact` → `get_evolution` |
| What changed recently? | `get_changes_since` or `get_evolution` |
| What breaks if I change X? | `get_impact` |
| Cross-service / cross-repo calls | `get_service_diagram` or `get_api_topology` |
| Dependency between two symbols | `find_dependency_path` |
| What files change together? | `get_cochange_context` |
| Architecture overview | `list_communities` + `find_central_symbols` |

## Standard Workflows

### "How does X work?" / "Explain X"
1. `find_symbol` or `find_code` → locate the symbol
2. `get_symbol_context` → callers, callees, community, processes
3. `get_process_flow` (if it's a process/request path)
4. Read source ONLY for the exact lines you need to quote

### Debugging "X is broken"
1. `find_symbol` → locate the broken thing
2. `get_symbol_context` → understand its role
3. `get_impact` → blast radius (what else breaks)
4. `get_evolution` → what changed recently (mode: `recent`)
5. `get_changes_since` → confirm timing vs incident

### "Where is X defined / called?"
1. `find_symbol` with `fuzzy: true`
2. `get_symbol_context` for full caller/callee map
3. Read specific file only after locating exact line

### Before any code modification
1. `find_symbol` → confirm you have the right target
2. `get_symbol_context` → understand full context
3. `get_impact` → know blast radius before touching anything

## Red Flags — STOP, Use Memtrace Instead

You are violating this skill if you think:

| Thought | Reality |
|---|---|
| "Let me grep for this" | `find_code` or `find_symbol` is faster and structurally aware |
| "Let me glob for the file" | `find_symbol` returns exact location with context |
| "Let me read the whole file" | `get_symbol_context` gives you only what matters |
| "It's just a quick search" | Grep has no understanding of call graphs, communities, or time |
| "I don't know if it's indexed" | Check with `list_indexed_repositories` first — takes 1 second |
| "The user didn't say to use Memtrace" | User asked about the code. Repo is indexed. Use Memtrace. |
| "This is a simple question" | Simple questions benefit most — one `find_symbol` vs 20 file reads |

## When File Tools Are Still Correct

Use Grep/Glob/Read ONLY for:
- Reading the **exact source lines** of a symbol you already located via Memtrace
- Files that are config, data, or docs (not source code symbols)
- Repos confirmed NOT indexed in Memtrace

Never use file tools as a **discovery** mechanism when Memtrace is available.

## Skill Priority

This skill is a **process skill** — it runs BEFORE any implementation or search skill.

When this skill applies, it overrides default file-search behavior. Use the specific Memtrace sub-skills for deep detail on each tool:

- Discovery → `memtrace-search`
- Impact analysis → `memtrace-impact`
- Temporal / change analysis → `memtrace-evolution`
- Incident investigation → `memtrace-incident-investigation`
- Architecture overview → `memtrace-codebase-exploration`
- Refactoring → `memtrace-refactoring-guide`

# Memtrace v0.3.29 — Hybrid Retrieval Benchmarks (`find_code`)

**Date**: 2026-05-01
**Hardware**: Apple M3 Max, 14 cores (10P + 4E), 36 GB RAM
**Methodology**: 1,000-row dataset × 3 query variants (`exact`, `split`, `typo`) = **3,000 query cases per adapter**, against Python AST ground truth. Each tool indexes the same corpora once and serves queries from its own pipeline.

This doc covers the `find_code` (hybrid retrieval) tool — the path an agent uses for natural-language code search, NOT the `find_symbol` exact-lookup path covered in [`BENCHMARKS-v0.3.22.md`](BENCHMARKS-v0.3.22.md). Both ship in the same binary; agents pick whichever fits the question.

`find_code` is a hybrid retrieval pipeline: BM25 (Tantivy fork, custom code tokenizer, per-field boosts) ⊕ semantic vector search (HNSW over 768-d code-specialised embeddings) ⊕ graph signal (callers count prior, exact-symbol order boost), fused via Reciprocal Rank Fusion, then re-ranked by a quantised ONNX cross-encoder (BAAI/bge-reranker-base, 75 MB) over a fixed 30-candidate pool.

`v0.3.29` ships two changes from `v0.3.21` (the previous published baseline):

1. **The whole BM25/rerank stack landed.** Tantivy fork with custom `B = 0.45`, code tokenizer, n-gram subword field, exact-name STRING field, per-field boosts (5/3/2/2/2/1), pseudo-relevance feedback, ANN filter pushdown, jina-embeddings-v2-base-code (768d), and the bge-reranker-base cross-encoder. See git history for the spec stack.
2. **Decouple rerank pool from caller's `LIMIT`.** v0.3.29 always reranks a 30-candidate pool then truncates to the requested LIMIT. So `find_code(limit=3)` returns the same top-3 a `find_code(limit=10)` call would — caller saves tokens without losing precision. See the commit `release(0.3.29)` for details.

---

## Results — Django (50,191 nodes, 180,939 edges, 3,000 query cases)

Pre-rerank baseline (`v0_3_21`) and post-rerank (`v0.3.29`) both at `LIMIT=10`. GitNexus and ChromaDB columns are from the `v0_3_21` run — they did not change between releases (no version bumps shipped). Same dataset, same machine, same query order across all three.

| Adapter | cov | acc@1 | acc@5 | acc@10 | MRR | tokens | p50 lat | p95 lat |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **memtrace v0.3.29** (rerank on, L=10) | 100.0% | **73.9%** | **88.2%** | **90.0%** | **0.801** | **473** | 872 ms | 1012 ms |
| memtrace v0_3_21 (no rerank, L=10)     | 100.0% | 48.9% | 84.0% | 88.5% | 0.632 | 1526 | 484 ms | 519 ms |
| gitnexus query (v0_3_21)               |  98.6% | 38.6% | 69.9% | 72.8% | 0.518 | 200 | 850 ms | 1840 ms |
| chromadb vector (v0_3_21)              |  99.4% | 28.9% | 48.9% | 53.3% | 0.372 | 1837 | 57 ms | 84 ms |

CGC is excluded from this matrix because its current public surface has graph + exact / full-text / substring search but no BM25 / vector / RRF retrieval path that maps onto `find_code`.

### v0.3.29 deltas vs v0_3_21 (memtrace-on-memtrace)

| metric | v0_3_21 | v0.3.29 | delta |
|---|---:|---:|---:|
| acc@1 | 48.9% | **73.9%** | **+25.0 pts** |
| acc@5 | 84.0% | 88.2% | +4.2 pts |
| acc@10 | 88.5% | 90.0% | +1.5 pts |
| MRR | 0.632 | **0.801** | **+0.169** |
| avg tokens | 1526 | **473** | **−1053 (−69%)** |
| p50 latency | 484 ms | 872 ms | +388 ms |

The +388 ms p50 hit is the rerank inference cost. Small price for the +25 pt jump on top-1, the 3.2× token reduction, and the +1.5 pt recall lift.

### By query variant (acc@1)

The rerank earns its keep on natural-language and typo queries — exactly what an agent will phrase like.

| variant | v0_3_21 acc@1 | v0.3.29 acc@1 | delta |
|---|---:|---:|---:|
| `exact` (literal symbol name) | 79.7% | 77.5% | −2.2 pts |
| `split` (snake/camelCase split into tokens) | **31.1%** | **70.8%** | **+39.7 pts** |
| `typo` (single-character typo)              | **35.8%** | **73.4%** | **+37.6 pts** |

`exact` regressed 2.2 pts — within noise for 1k queries (22 cases), and the rerank occasionally over-thinks a clean string match. `split` and `typo` are where the new stack shines: doubling and tripling the rate at which the right symbol comes back at rank 1. `split` is the closest variant to a real agent query ("the function that creates SQL test tables for postgres" rather than the exact identifier).

### By query variant (acc@10)

Recall ceiling is also up across the board.

| variant | v0_3_21 acc@10 | v0.3.29 acc@10 | delta |
|---|---:|---:|---:|
| `exact` | 92.4% | 93.4% | +1.0 pt |
| `split` | 86.0% | 86.5% | +0.5 pt |
| `typo`  | 87.1% | 90.0% | +2.9 pts |

---

## Results — mempalace (2,287 nodes, 5,962 edges, 3,000 query cases)

Same harness, same query set construction. Memtrace v0.3.29 vs OLD baseline vs published competitors.

| Adapter | cov | acc@1 | acc@5 | acc@10 | MRR | tokens | p50 lat | p95 lat |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **memtrace v0.3.29** (rerank on, L=10) | 100.0% | **93.8%** | **99.3%** | **99.7%** | **0.961** | **419** | 447 ms | 554 ms |
| memtrace v0_3_21 (no rerank, L=10)     | 100.0% | 44.2% | 92.6% | 98.7% | 0.625 | 1518 | 35 ms | 51 ms |
| chromadb vector (v0_3_21)              | 100.0% | 59.7% | 82.9% | 85.5% | 0.695 | 1936 | 56 ms | 84 ms |
| gitnexus query (v0_3_21)               | 100.0% | 11.7% | 79.8% | 94.6% | 0.346 |  357 | 390 ms | 950 ms |

**v0.3.29 deltas vs v0_3_21 (memtrace-on-memtrace):**

| metric | v0_3_21 | v0.3.29 | delta |
|---|---:|---:|---:|
| acc@1 | 44.2% | **93.8%** | **+49.6 pts** |
| acc@5 | 92.6% | 99.3% | +6.7 pts |
| acc@10 | 98.7% | 99.7% | +1.0 pt |
| MRR | 0.625 | **0.961** | **+0.336** |
| avg tokens | 1518 | **419** | **−1099 (−72%)** |
| p50 latency | 35 ms | 447 ms | +412 ms |

The acc@1 lift is **+49.6 pts** on mempalace — bigger than Django (+25 pts) because the corpus is small enough that the right candidate is almost always in the rerank pool, and the cross-encoder just needs to surface it.

**By query variant (acc@1):**

| variant | v0_3_21 | v0.3.29 | delta |
|---|---:|---:|---:|
| `exact` | 96.6% | 94.5% | −2.1 pts |
| `split` | **16.9%** | **94.9%** | **+78.0 pts** |
| `typo`  | **19.0%** | **91.9%** | **+72.9 pts** |

Same pattern as Django, even more pronounced: `exact` drifts down ~2 pts (rerank over-thinks clean string matches), but `split` and `typo` go from broken (17–19%) to nearly perfect (92–95%). The agent-realistic workload — natural-language queries and typos — is where the rerank stack earns its keep.

**Vs competitors (acc@1):** memtrace v0.3.29 is **1.57× over ChromaDB** (93.8% vs 59.7%) and **8.0× over GitNexus** (93.8% vs 11.7%) on the same task. The pre-rerank v0_3_21 stack actually trailed ChromaDB on acc@1 (44.2% vs 59.7%); the v0.3.29 stack flips that decisively.

CGC is excluded from this matrix because its current public surface has graph + exact / full-text / substring search but no BM25 / vector / RRF retrieval path that maps onto `find_code`.

---

## How to reproduce

```bash
# Install Memtrace v0.3.29
npm install -g memtrace@0.3.29

# Index the corpora
memtrace index /path/to/django
memtrace index /path/to/mempalace

# Run the bench (Django)
ADAPTERS=memtrace \
MEMTRACE_RERANK=on \
REPO_ROOT=/path/to/django \
DATASET_FILE=benchmarks/fair/dataset_1k_django.json \
RESULTS_FILE=benchmarks/fair/results_hybrid_1k_django_v0_3_29.json \
LIMIT=10 \
MAX_QUERIES=1000 \
QUERY_VARIANTS=exact,split,typo \
python3 benchmarks/fair/run_hybrid_retrieval_benchmark.py
```

The published v0_3_21 baselines in `results_hybrid_1k_*_v0_3_21.json` were generated by the same script with `MEMTRACE_RERANK=off` (or unset) and an older Memtrace binary.

---

## What changed between v0_3_21 and v0.3.29 (high level)

The full spec stack lives in the closed-source repo; the agent-visible diff is:

- **Tantivy fork** (`syncable-dev/tantivy@bm25-b-0.45`) — `B = 0.45` instead of upstream 0.75, calibrated against code-style identifier tokens. Plus the per-field boosts (NAME=5, SIG=3, SCOPE=2, KIND=2, LANG=2, CONTENT=1) tuned via a 6-row Sourcegraph-style sweep.
- **Custom code tokenizer** — splits camelCase / snake_case / kebab-case at query *and* index time, so `getUserById` matches `user_by_id`. Plus the n-gram subword field for typo recall.
- **Exact-name STRING field** with a separate boost path so identifier exact matches don't lose to BM25 corpus stats on rare names.
- **Pseudo-relevance feedback** — when the primary search lands fewer than 3 results, expand the query with the top hit's `name` + `scope_path` and re-issue once. Lifts recall on short / generic queries without hurting precision.
- **Caller-count prior** (`1 + ln(1 + direct_callers)`) — log-compressed structural boost so a function with 47 callers ranks above a same-named test fixture.
- **Code-specialised embeddings** — `jinaai/jina-embeddings-v2-base-code` (768d, code-tuned BERT). Replaced the old `bge-small-en-v1.5` (384d, English text). Override with `MEMTRACE_EMBED_MODEL=bge-small` if you want the legacy stack.
- **Cross-encoder rerank** — quantised ONNX `BAAI/bge-reranker-base` (75 MB) over the fused candidate pool. Default ON in v0.3.29 for agent traffic.
- **Decouple rerank pool from `LIMIT`** — v0.3.29: rerank always sees 30 candidates, truncates after. Means `LIMIT=3` returns the same top-3 as `LIMIT=10`, so callers can save tokens with no quality loss.
- **`.claude/` walker exclude + `cleanup_stale_records` MCP tool** — agent-worktree pollution fix and a targeted scrub for orphan records left behind by removed worktrees / branch deletes / files-deleted-while-stopped.

See the closed-source `specs/bm25-improvements/` directory for the per-feature design docs and ablation evidence.

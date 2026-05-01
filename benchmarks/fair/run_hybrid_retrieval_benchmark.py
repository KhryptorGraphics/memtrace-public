#!/usr/bin/env python3
"""
Hybrid retrieval benchmark for code-search systems.

This is intentionally separate from `run_fair_benchmark_v2.py`.
That older harness measures exact symbol lookup (`find_symbol`-style
queries). This harness measures retrieval behavior:

  - Memtrace: `find_code` (BM25 + vector + graph/RRF path)
  - GitNexus: `query` tool (BM25 + vector + RRF + process grouping)
  - ChromaDB: vector-only baseline over 800-char source chunks

The dataset is the same fair benchmark JSON used by previous runs:
each row has a target symbol and expected file. For each row we create
multiple query variants so the benchmark exercises more than exact-name
lookup:

  - exact: target symbol as-is
  - split: snake/camel tokens as a phrase
  - typo: deterministic typo in the target symbol

Environment:

  REPO_ROOT=/path/to/repo
  DATASET_FILE=benchmarks/fair/dataset_1k_django.json
  RESULTS_FILE=benchmarks/fair/results_hybrid_1k_django.json
  ADAPTERS=memtrace,gitnexus,chromadb
  QUERY_VARIANTS=exact,split,typo
  MAX_QUERIES=1000
  LIMIT=10

GitNexus:
  The script starts `gitnexus eval-server` if no server is reachable at
  `GN_URL` (default http://127.0.0.1:4848/tool/query). Existing global
  GitNexus indexes are reused.
"""

from __future__ import annotations

import json
import os
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib import request

HERE = Path(__file__).parent
ROOT = HERE.parent.parent

_repo_default = os.environ.get("REPO_ROOT")
if not _repo_default:
    raise SystemExit(
        "REPO_ROOT is required. Set it to the on-disk path of the corpus "
        "you've already indexed with `memtrace index`. Example:\n"
        "  REPO_ROOT=/path/to/django python benchmarks/fair/run_hybrid_retrieval_benchmark.py"
    )
REPO_ROOT = Path(_repo_default)
REPO_PARENT = REPO_ROOT.parent
REPO_NAME = REPO_ROOT.name

DATASET_FILE = Path(os.environ.get("DATASET_FILE", str(HERE / "dataset.json")))
RESULTS_FILE = Path(
    os.environ.get("RESULTS_FILE", str(HERE / f"results_hybrid_{REPO_NAME}.json"))
)

# Resolve the memtrace binary. Order: explicit MEMTRACE_BIN env var → first
# `memtrace` on PATH (e.g. `npm install -g memtrace`) → local cargo build
# under `target/release/memtrace` if the user is running from a source checkout.
def _resolve_memtrace_bin() -> str:
    env = os.environ.get("MEMTRACE_BIN")
    if env:
        return env
    import shutil
    on_path = shutil.which("memtrace")
    if on_path:
        return on_path
    return str(ROOT / "target" / "release" / "memtrace")

MEMTRACE_BIN = _resolve_memtrace_bin()
MEMTRACE_BACKEND = os.environ.get("MEMTRACE_BACKEND", "memdb-current")
GN_URL = os.environ.get("GN_URL", "http://127.0.0.1:4848/tool/query")
GN_HEALTH_URL = os.environ.get("GN_HEALTH_URL", "http://127.0.0.1:4848/health")
GN_PORT = int(os.environ.get("GN_PORT", "4848"))

MAX_QUERIES = int(os.environ.get("MAX_QUERIES", "1000"))
LIMIT = int(os.environ.get("LIMIT", "10"))
SOURCE_CONTEXT_BEFORE_LINES = int(os.environ.get("SOURCE_CONTEXT_BEFORE_LINES", "80"))
SOURCE_CONTEXT_AFTER_LINES = int(os.environ.get("SOURCE_CONTEXT_AFTER_LINES", "120"))
QUERY_VARIANTS = [
    v.strip()
    for v in os.environ.get("QUERY_VARIANTS", "exact,split,typo").split(",")
    if v.strip()
]
ADAPTERS_TO_RUN = {
    a.strip()
    for a in os.environ.get("ADAPTERS", "memtrace,gitnexus,chromadb").split(",")
    if a.strip()
}

SOURCE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".rb",
    ".php",
    ".dart",
    ".scala",
    ".pl",
}

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    "target",
    ".memdb",
    ".gitnexus",
    ".codegraphcontext",
}


@dataclass(frozen=True)
class QueryCase:
    id: str
    variant: str
    query: str
    target_symbol: str
    expected_file: str


def _self_rss_kb() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = usage.ru_maxrss
    # macOS returns bytes; Linux returns KB.
    if rss > 10 * 1024 * 1024:
        return rss // 1024
    return rss


class RSSWatcher:
    def __init__(self, interval_s: float = 0.25):
        self.interval_s = interval_s
        self.peak_kb = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.peak_kb = _self_rss_kb()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak_kb = max(self.peak_kb, _self_rss_kb())
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def peak_mb(self) -> float:
        return round(self.peak_kb / 1024, 1)


def split_identifier(value: str) -> str:
    """Turn `findHTTPServer_error` into `find HTTP Server error`."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"[_\-.:/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or value


def deterministic_typo(value: str) -> str:
    letters = [i for i, ch in enumerate(value) if ch.isalpha()]
    if len(letters) < 3:
        return value + "x"
    mid = letters[len(letters) // 2]
    # Drop one internal alpha char. This keeps the query readable while
    # forcing typo/partial-recall behavior.
    return value[:mid] + value[mid + 1 :]


def make_query_cases(dataset: list[dict], max_queries: int, variants: list[str]) -> list[QueryCase]:
    out: list[QueryCase] = []
    for row in dataset[:max_queries]:
        target = row["target_symbol"]
        expected = row["expected_file"]
        for variant in variants:
            if variant == "exact":
                query = target
            elif variant == "split":
                query = split_identifier(target)
            elif variant == "typo":
                query = deterministic_typo(target)
            else:
                raise ValueError(f"unknown query variant: {variant}")
            out.append(
                QueryCase(
                    id=f"{row['id']}:{variant}",
                    variant=variant,
                    query=query,
                    target_symbol=target,
                    expected_file=expected,
                )
            )
    return out


def normalize_path(path_value: str) -> str:
    if not path_value:
        return ""
    p = path_value.strip().replace("\\", "/")
    # Strip URL/markdown punctuation commonly present in text-formatted tool output.
    p = p.strip("`'\"()[]{}<>,.;")
    if not p:
        return ""

    if p.startswith(str(REPO_PARENT).replace("\\", "/")):
        try:
            return str(Path(p).resolve().relative_to(REPO_PARENT)).replace("\\", "/")
        except Exception:
            pass

    marker = f"/{REPO_NAME}/"
    if marker in p:
        return f"{REPO_NAME}/{p.split(marker, 1)[1]}"
    if p.startswith(f"{REPO_NAME}/"):
        return p
    if p.startswith("./"):
        p = p[2:]
    first_segment = p.split("/", 1)[0]
    if "/" in p and (REPO_PARENT / first_segment).exists():
        return p
    if p.startswith("/"):
        return p.lstrip("/")
    return f"{REPO_NAME}/{p}" if not p.startswith(f"{REPO_NAME}/") else p


PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.$~+\-]+/)+[A-Za-z0-9_.$~+\-]+\.(?:py|pyi|ts|tsx|js|jsx|rs|go|java|c|cc|cpp|h|hpp|cs|swift|kt|rb|php|dart|scala|pl))(?:[:#]\d+)?"
)


def parse_paths_from_text(text: str) -> list[str]:
    paths: list[str] = []
    for match in PATH_RE.finditer(text):
        norm = normalize_path(match.group("path"))
        if norm and norm not in paths:
            paths.append(norm)
    return paths


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def resolve_source_file(path_value: str) -> Path | None:
    if not path_value:
        return None
    raw = Path(path_value)
    if raw.is_absolute() and raw.is_file():
        return raw

    norm = normalize_path(path_value)
    candidates: list[Path] = []
    if norm.startswith(f"{REPO_NAME}/"):
        candidates.append(REPO_PARENT / norm)
    candidates.append(REPO_ROOT / norm)
    candidates.append(REPO_PARENT / norm)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((max(1, s), max(s, e)) for s, e in spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def estimate_source_baseline_tokens(refs: list[dict]) -> int:
    """Estimate source windows an agent would read after resolving these hits.

    This mirrors the MCP value ledger: graph results give exact AST spans, but a
    grep/vector workflow usually needs a surrounding file window. Counting the
    serialized Memtrace JSON alone is response size, not source context avoided.
    """
    coverage: dict[Path, list[tuple[int, int]] | None] = {}
    for ref in refs:
        path = resolve_source_file(str(ref.get("file_path") or ref.get("path") or ""))
        if not path:
            continue
        start = ref.get("start_line")
        end = ref.get("end_line") or start
        if not isinstance(start, int) or start <= 0:
            coverage[path] = None
            continue
        if path in coverage and coverage[path] is None:
            continue
        expanded = (
            max(1, start - SOURCE_CONTEXT_BEFORE_LINES),
            max(start, int(end)) + SOURCE_CONTEXT_AFTER_LINES,
        )
        coverage.setdefault(path, []).append(expanded)

    total_chars = 0
    for path, spans in coverage.items():
        try:
            if spans is None:
                total_chars += path.stat().st_size
                continue
            merged = _merge_spans(spans)
            with path.open("r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, start=1):
                    if not merged:
                        break
                    while merged and line_no > merged[0][1]:
                        merged.pop(0)
                    if merged and merged[0][0] <= line_no <= merged[0][1]:
                        total_chars += len(line)
        except OSError:
            continue
    return total_chars // 4


def score_one(expected_file: str, paths: list[str]) -> dict:
    expected = normalize_path(expected_file)
    for i, p in enumerate(paths[:LIMIT], start=1):
        got = normalize_path(p)
        if not got:
            continue
        if got == expected or got.endswith(expected) or expected.endswith(got):
            return {"rank": i, "hit_in": f"top_{i}"}
    return {"rank": None, "hit_in": None}


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * q), len(ordered) - 1)]


def summarise(name, desc, results, wall, peak_mb, index_time_s, embedding_time_s):
    n = len(results)
    if n == 0:
        return None
    covered = [r for r in results if r["paths_count"] > 0]
    hit_at_1 = sum(1 for r in results if r["rank"] == 1)
    hit_at_5 = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 5)
    hit_at_10 = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 10)
    mrr = sum(1.0 / r["rank"] for r in results if r["rank"] is not None) / n
    precision_at_10 = sum(
        (1 if r["rank"] is not None and r["rank"] <= 10 else 0)
        / (min(r["paths_count"], 10) or 1)
        for r in results
    ) / n
    lats = [r["latency_ms"] for r in results]

    by_variant: dict[str, dict] = {}
    for variant in sorted({r["variant"] for r in results}):
        rows = [r for r in results if r["variant"] == variant]
        vn = len(rows)
        by_variant[variant] = {
            "n_queries": vn,
            "coverage_pct": round(sum(1 for r in rows if r["paths_count"] > 0) / vn * 100, 2),
            "acc_at_1_pct": round(sum(1 for r in rows if r["rank"] == 1) / vn * 100, 2),
            "acc_at_10_pct": round(
                sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 10) / vn * 100,
                2,
            ),
            "mrr": round(sum(1.0 / r["rank"] for r in rows if r["rank"] is not None) / vn, 4),
        }

    return {
        "adapter": name,
        "description": desc,
        "n_queries": n,
        "query_variants": QUERY_VARIANTS,
        "coverage_pct": round(len(covered) / n * 100, 2),
        "acc_at_1_pct": round(hit_at_1 / n * 100, 2),
        "acc_at_5_pct": round(hit_at_5 / n * 100, 2),
        "acc_at_10_pct": round(hit_at_10 / n * 100, 2),
        "recall_at_10_pct": round(hit_at_10 / n * 100, 2),
        "precision_at_10": round(precision_at_10, 4),
        "mrr": round(mrr, 4),
        "conditional_acc_at_1_pct": round(
            (sum(1 for r in covered if r["rank"] == 1) / len(covered) * 100) if covered else 0.0,
            2,
        ),
        "avg_latency_ms": round(statistics.mean(lats), 2),
        "median_latency_ms": round(statistics.median(lats), 2),
        "p95_latency_ms": round(percentile(lats, 0.95), 2),
        "p99_latency_ms": round(percentile(lats, 0.99), 2),
        "avg_tokens": round(statistics.mean(r["tokens"] for r in results), 0),
        "avg_response_tokens": round(statistics.mean(r.get("response_tokens", r["tokens"]) for r in results), 0),
        "avg_source_payload_tokens": round(statistics.mean(r.get("source_payload_tokens", 0) for r in results), 0),
        "avg_source_baseline_tokens": round(statistics.mean(r.get("source_baseline_tokens", 0) for r in results), 0),
        "avg_context_tokens_avoided_est": round(
            statistics.mean(r.get("context_tokens_avoided_est", 0) for r in results),
            0,
        ),
        "source_avoidance_rate_pct": round(
            (
                sum(r.get("context_tokens_avoided_est", 0) for r in results)
                / max(sum(r.get("source_baseline_tokens", 0) for r in results), 1)
                * 100
            ),
            2,
        ),
        "wall_seconds": round(wall, 2),
        "peak_rss_mb": peak_mb,
        "index_time_s": index_time_s,
        "embedding_time_s": embedding_time_s,
        "by_variant": by_variant,
    }


class MemtraceFindCodeAdapter:
    name = "memtrace_find_code"
    description = f"Memtrace find_code, hybrid BM25 + vector + graph/RRF — backend={MEMTRACE_BACKEND}"

    def __init__(self):
        self.index_time_s = float(os.environ.get("MEMTRACE_INDEX_TIME_S", "0.0"))
        self.embedding_time_s = float(os.environ.get("MEMTRACE_EMBEDDING_TIME_S", "0.0"))
        env = os.environ.copy()
        env.setdefault("MEMTRACE_DEV", "1")
        env.setdefault("MEMTRACE_TELEMETRY", "off")
        self.p = subprocess.Popen(
            [MEMTRACE_BIN, "mcp"],
            cwd=str(ROOT / "benchmarks" / "index_workspace"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hybrid-fair-bench", "version": "1.0"},
            },
        )
        self._notify("notifications/initialized")

    def _rpc(self, method: str, params: dict):
        rid = str(uuid.uuid4())
        assert self.p.stdin is not None
        assert self.p.stdout is not None
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg

    def _notify(self, method: str, params: dict | None = None):
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()

    def query(self, case: QueryCase):
        t0 = time.time()
        resp = self._rpc(
            "tools/call",
            {
                "name": "find_code",
                "arguments": {"query": case.query, "repo_id": REPO_NAME, "limit": LIMIT},
            },
        )
        latency_ms = (time.time() - t0) * 1000
        text = ""
        paths: list[str] = []
        source_refs: list[dict] = []
        if resp and "result" in resp:
            for c in resp["result"].get("content", []):
                if c.get("type") == "text":
                    text += c.get("text", "")
            try:
                data = json.loads(text)
                for r in data.get("results", []):
                    fp = normalize_path(r.get("file_path", "") or "")
                    if fp and fp not in paths:
                        paths.append(fp)
                    if r.get("file_path"):
                        source_refs.append(
                            {
                                "file_path": r.get("file_path"),
                                "start_line": r.get("start_line"),
                                "end_line": r.get("end_line"),
                            }
                        )
            except json.JSONDecodeError:
                paths = parse_paths_from_text(text)
        response_tokens = estimate_tokens(text)
        source_baseline_tokens = estimate_source_baseline_tokens(source_refs)
        return {
            "paths": paths[:LIMIT],
            "latency_ms": latency_ms,
            "tokens": response_tokens,
            "response_tokens": response_tokens,
            "source_payload_tokens": 0,
            "source_baseline_tokens": source_baseline_tokens,
            "context_tokens_avoided_est": max(0, source_baseline_tokens - response_tokens),
        }

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except Exception:
                self.p.kill()


class GitNexusQueryAdapter:
    name = "gitnexus_query"
    description = "GitNexus query tool, BM25 + semantic + RRF + process grouping"

    def __init__(self):
        self.index_time_s = float(os.environ.get("GN_INDEX_TIME_S", "0.0"))
        self.embedding_time_s = float(os.environ.get("GN_EMBEDDING_TIME_S", "0.0"))
        self.proc: subprocess.Popen | None = None
        if not self._server_up():
            self._start_server()
        self.server_up = self._server_up()

    def _server_up(self) -> bool:
        try:
            with request.urlopen(GN_HEALTH_URL, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _start_server(self) -> None:
        self.proc = subprocess.Popen(
            ["gitnexus", "eval-server", "--port", str(GN_PORT), "--idle-timeout", "0"],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("gitnexus eval-server exited before ready")
            if self._server_up():
                return
            time.sleep(0.25)
        raise TimeoutError("gitnexus eval-server did not become ready")

    def query(self, case: QueryCase):
        if not self.server_up:
            return {"paths": [], "latency_ms": 0.0, "tokens": 0, "unavailable": True}
        t0 = time.time()
        text = ""
        try:
            req = request.Request(
                GN_URL,
                data=json.dumps(
                    {
                        "query": case.query,
                        "repo": REPO_NAME,
                        "targetDir": str(REPO_ROOT),
                        "limit": LIMIT,
                        "max_symbols": LIMIT,
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", errors="ignore")
            paths = parse_paths_from_text(text)
        except Exception as e:
            text = f"<error: {e}>"
            paths = []
        response_tokens = estimate_tokens(text)
        return {
            "paths": paths[:LIMIT],
            "latency_ms": (time.time() - t0) * 1000,
            "tokens": response_tokens,
            "response_tokens": response_tokens,
            "source_payload_tokens": 0,
            "source_baseline_tokens": 0,
            "context_tokens_avoided_est": 0,
        }

    def preflight(self, cases: list[QueryCase]) -> tuple[bool, str | None]:
        # GitNexus can start successfully while its hybrid `query` lane is unusable.
        # On the current local 1.6.3 install, embeddings remain at 0 after
        # `analyze --embeddings`, and the eval server tries to create FTS indexes
        # lazily from a read-only DB. Exact-symbol preflight catches that state.
        exact_cases = [case for case in cases if case.variant == "exact"][:5] or cases[:5]
        checked = 0
        for case in exact_cases:
            checked += 1
            out = self.query(case)
            if out.get("paths"):
                return True, None
        return (
            False,
            "GitNexus query returned no file paths for exact-symbol preflight "
            f"({checked} checked). Local GitNexus metadata also shows embeddings=0; "
            "treating this lane as unavailable instead of reporting invalid zero-score results.",
        )

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                request.urlopen(
                    request.Request(
                        f"http://127.0.0.1:{GN_PORT}/shutdown",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    timeout=2,
                ).read()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.terminate()


class ChromaDBAdapter:
    name = "chromadb_vector"
    description = "ChromaDB vector-only baseline, 800-char source chunks"

    def __init__(self):
        import chromadb

        self.client = chromadb.Client()
        collection_name = f"hybrid_fair_{REPO_NAME}_{uuid.uuid4().hex[:8]}"
        self.col = self.client.create_collection(collection_name)
        self.index_time_s = 0.0
        self.embedding_time_s = 0.0
        self._index()

    def _index(self):
        print(f"  [chromadb] indexing {REPO_NAME} source chunks...")
        docs: list[str] = []
        ids: list[str] = []
        metas: list[dict] = []
        idx = 0
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for fname in files:
                fpath = Path(root) / fname
                if fpath.suffix not in SOURCE_EXTENSIONS:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                rel = str(fpath.relative_to(REPO_PARENT)).replace("\\", "/")
                for i in range(0, len(content), 800):
                    chunk = content[i : i + 800]
                    if len(chunk.strip()) < 20:
                        continue
                    docs.append(chunk)
                    ids.append(f"c{idx}")
                    metas.append({"source": rel})
                    idx += 1
        t0 = time.time()
        batch = 500
        for i in range(0, len(docs), batch):
            self.col.add(documents=docs[i : i + batch], ids=ids[i : i + batch], metadatas=metas[i : i + batch])
        self.embedding_time_s = round(time.time() - t0, 2)
        self.index_time_s = self.embedding_time_s
        print(f"  [chromadb] indexed {len(docs)} chunks in {self.index_time_s:.1f}s")

    def query(self, case: QueryCase):
        t0 = time.time()
        res = self.col.query(query_texts=[case.query], n_results=LIMIT)
        latency_ms = (time.time() - t0) * 1000
        paths: list[str] = []
        metas = res.get("metadatas", [[]])[0] if res.get("metadatas") else []
        docs = res.get("documents", [[]])[0] if res.get("documents") else []
        for meta in metas:
            src = normalize_path(meta.get("source", "") if meta else "")
            if src and src not in paths:
                paths.append(src)
        source_payload_tokens = sum(len(d or "") for d in docs) // 4
        return {
            "paths": paths[:LIMIT],
            "latency_ms": latency_ms,
            "tokens": source_payload_tokens,
            "response_tokens": source_payload_tokens,
            "source_payload_tokens": source_payload_tokens,
            "source_baseline_tokens": source_payload_tokens,
            "context_tokens_avoided_est": 0,
        }

    def close(self):
        pass


def make_adapters():
    builders = {
        "memtrace": MemtraceFindCodeAdapter,
        "memtrace_find_code": MemtraceFindCodeAdapter,
        "gitnexus": GitNexusQueryAdapter,
        "gitnexus_query": GitNexusQueryAdapter,
        "chromadb": ChromaDBAdapter,
        "chromadb_vector": ChromaDBAdapter,
    }
    out = []
    for name in ["memtrace", "gitnexus", "chromadb"]:
        if name in ADAPTERS_TO_RUN or builders[name].name in ADAPTERS_TO_RUN:
            try:
                out.append(builders[name]())
            except Exception as e:
                print(f"  [{name}] build failed: {e}")
    return out


def run_adapter(adapter, cases: list[QueryCase]):
    results = []
    rss = RSSWatcher()
    rss.start()
    t0 = time.time()
    try:
        for i, case in enumerate(cases):
            out = adapter.query(case)
            if out.get("unavailable"):
                rss.stop()
                return None
            scored = score_one(case.expected_file, out["paths"])
            results.append(
                {
                    "id": case.id,
                    "variant": case.variant,
                    "query": case.query,
                    "target": case.target_symbol,
                    "expected_file": normalize_path(case.expected_file),
                    "paths_count": len(out["paths"]),
                    "top_paths": out["paths"][:3],
                    "rank": scored["rank"],
                    "latency_ms": out["latency_ms"],
                    "tokens": out["tokens"],
                    "response_tokens": out.get("response_tokens", out["tokens"]),
                    "source_payload_tokens": out.get("source_payload_tokens", 0),
                    "source_baseline_tokens": out.get("source_baseline_tokens", 0),
                    "context_tokens_avoided_est": out.get("context_tokens_avoided_est", 0),
                }
            )
            if (i + 1) % 250 == 0:
                acc1 = sum(1 for r in results if r["rank"] == 1) / len(results) * 100
                acc10 = sum(1 for r in results if r["rank"] is not None and r["rank"] <= 10) / len(results) * 100
                cov = sum(1 for r in results if r["paths_count"] > 0) / len(results) * 100
                print(f"  [{adapter.name}] {i+1}/{len(cases)} acc@1={acc1:.1f}% acc@10={acc10:.1f}% cov={cov:.1f}%")
    finally:
        rss.stop()
        adapter.close()
    return {"results": results, "wall_seconds": time.time() - t0, "peak_rss_mb": rss.peak_mb}


def main() -> int:
    if not REPO_ROOT.exists():
        print(f"repo not found: {REPO_ROOT}", file=sys.stderr)
        return 2
    if not DATASET_FILE.exists():
        print(f"dataset not found: {DATASET_FILE}", file=sys.stderr)
        return 2

    dataset = json.loads(DATASET_FILE.read_text())
    cases = make_query_cases(dataset, min(MAX_QUERIES, len(dataset)), QUERY_VARIANTS)
    print(
        f"Hybrid retrieval benchmark — {len(cases)} query cases "
        f"({min(MAX_QUERIES, len(dataset))} dataset rows × {len(QUERY_VARIANTS)} variants) "
        f"on {REPO_NAME}\n"
    )

    report = {
        "repo": REPO_NAME,
        "repo_path": str(REPO_ROOT),
        "dataset": str(DATASET_FILE),
        "n_dataset_rows": min(MAX_QUERIES, len(dataset)),
        "n_query_cases": len(cases),
        "limit": LIMIT,
        "query_variants": QUERY_VARIANTS,
        "adapters_requested": sorted(ADAPTERS_TO_RUN),
        "notes": {
            "cgc": "CGC is excluded from this hybrid benchmark because current CGC source has graph + exact/full-text/substring search, but no BM25/vector/RRF retrieval path.",
        },
        "tools": {},
    }

    adapters = make_adapters()
    for adapter in adapters:
        print(f"\n── {adapter.name} ── {adapter.description}")
        preflight = getattr(adapter, "preflight", None)
        if preflight is not None:
            ok, reason = preflight(cases)
            if not ok:
                print(f"  [{adapter.name}] UNAVAILABLE — {reason}")
                report["tools"][adapter.name] = {
                    "status": "unavailable",
                    "reason": reason,
                }
                adapter.close()
                continue
        result = run_adapter(adapter, cases)
        if result is None:
            print(f"  [{adapter.name}] UNAVAILABLE — skipped")
            report["tools"][adapter.name] = {"status": "unavailable"}
            continue
        summary = summarise(
            adapter.name,
            adapter.description,
            result["results"],
            result["wall_seconds"],
            result["peak_rss_mb"],
            getattr(adapter, "index_time_s", 0.0),
            getattr(adapter, "embedding_time_s", 0.0),
        )
        report["tools"][adapter.name] = {
            **summary,
            "sample_results": result["results"][:10],
        }
        print(json.dumps(summary, indent=2))

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(report, indent=2))
    print(f"\n✓ wrote {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

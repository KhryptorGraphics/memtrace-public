# Privacy and telemetry

A practical summary of what stays on your machine, what's optionally
sent to us, and how to turn anything off. The exhaustive
machine-readable version lives in
[`PRIVACY.md`](../PRIVACY.md) and [`TELEMETRY.md`](../TELEMETRY.md)
at the repo root — those are the contractual versions for legal /
compliance reviews. This doc is the user-friendly explainer.

## What never leaves your machine

- **Your source code.** Indexing, parsing, embedding, ranking — all
  local. No file content is uploaded anywhere.
- **File paths and symbol names.** Stays on disk in your
  `.memdb/` and `~/.memtrace/embed-cache/`. Never transmitted.
- **Repo structure.** Module names, community detection, call graphs
  — all in MemDB on your disk.
- **Secrets and `.env` files.** Memtrace doesn't even index `.env`
  by default (it's not parseable code). Pattern-matched detection
  during Terraform emission flags secret-looking properties.
- **Search queries.** When the agent calls `find_code(query="...")`,
  the query is processed locally. We never see it.
- **Agent conversation history.** That's between you and your AI
  client. Memtrace is invisible to both directions of that channel.

## What does cross the network

Three categories. Two are required for the product to work; one is
optional.

### 1. License validation (required)

On startup and roughly every hour, Memtrace pings our license
service to confirm your session token is still valid. The request
contains:

- A device hash (SHA-256 of stable hardware id + a salt — not
  reversible to your machine identity)
- The product version
- The session token issued at first-run device-flow login

It does NOT contain:

- Repo paths
- File or symbol names
- Query content
- Code

If your machine is offline, Memtrace runs in a grace-period mode
for 24 hours before requiring re-validation. CI / sandboxed
environments use `MEMTRACE_LICENSE_KEY=<key>` instead of device flow.

### 2. Crash and error reports (required, anonymised)

When the daemon crashes or hits a fatal error, we receive a
sanitised report:

- The error string (with all paths replaced by `<path>` placeholders)
- Stack trace function names (no source locations beyond the function
  name itself)
- The product version, OS, arch
- The same anonymous device hash

It does NOT contain:

- Source code
- File contents
- Repo paths
- User-supplied identifiers

This is what lets us actually fix bugs that hit you. The sanitiser
is conservative: when in doubt, redact.

### 3. Aggregate usage telemetry (OPT-IN, off by default)

If — and only if — you opt in during `memtrace start`, the daemon
sends a daily aggregate ping. The ping is one HTTPS POST per 24h
containing aggregate counts:

| Category | What's in it |
|---|---|
| Identity | Device hash (same one as license) |
| Environment | Product version, OS, architecture, install method |
| Usage volume | Tool call counts (how many `find_code` calls in 24h, etc.), tokens-saved estimates |
| Quality | Top 5 commands with low / zero savings (so we know what to improve), parse failure counts |
| Ecosystem mix | Tool category distribution (e.g. git 45%, python 20%, …) |
| Retention | Days since first use, active days in last 30 |
| Configuration | Whether `config.toml` exists, count of excluded commands |
| Economics | Estimated USD savings (based on public token pricing) |

Specifically NOT collected even when telemetry is ON:

- Source code
- File paths
- Symbol names
- Command arguments (only command names, like "git" or "cargo")
- Query strings
- Anything from your repo

Top-command reports name only the tool ("git", "cargo", "pytest"),
never the full command line.

## How to control telemetry

```bash
memtrace telemetry status      # current state — granted, disabled, or unset
memtrace telemetry enable      # explicit consent (interactive prompt)
memtrace telemetry disable     # withdraw consent — stops collection immediately
memtrace telemetry forget      # disable + delete local telemetry data + request server-side erasure
```

Hard env override (highest precedence; works even if consent is
granted):

```bash
export MEMTRACE_TELEMETRY_DISABLED=1
```

If you set this in your shell init, telemetry is off everywhere
regardless of what the consent state says.

## How to inspect what would be sent

Telemetry events are buffered locally before being sent. While the
daemon is running:

```bash
ls -la ~/.memtrace/telemetry/
```

You can `cat` any pending event file — it's JSON, human-readable.
If you opt out, this directory doesn't exist or is empty.

## Default state

| Item | Default |
|---|---|
| License validation | ON (required for the product to work) |
| Crash + error reports | ON (anonymised) |
| Aggregate usage telemetry | **OFF** — explicit opt-in required |

The opt-in prompt during `memtrace start` shows the full data list
above and the URL of the privacy policy. You can decline and use
the product fully.

## Where the data goes

All telemetry endpoints terminate at our own infrastructure
(`*.memtrace.io`), not third-party analytics services. The full
data-handling commitments — retention, who has access, when it's
deleted, GDPR rights — are in [`PRIVACY.md`](../PRIVACY.md). If
your organisation has compliance requirements (SOC 2 questionnaire,
DPA, etc.), email `privacy@memtrace.io` and we'll provide the
documentation.

## Network egress summary

If you want to firewall Memtrace, the outbound destinations are:

- `*.memtrace.io` (license + telemetry)
- `huggingface.co` and `cdn-lfs*.huggingface.co` (model downloads,
  first-run only — cached locally after)
- `registry.npmjs.org` (only when running `memtrace install` to
  upgrade)

Block any of these and the product still runs (offline grace
period for license, no auto-upgrades, no model updates), it just
gets slowly less functional.

## TL;DR

- We never see your code, queries, or repo data.
- License validation needs a daily-ish ping with no content.
- Crash reports are anonymised — we redact paths, names, args.
- Aggregate usage telemetry is OFF by default. You have to
  explicitly turn it on. You can revoke at any time.
- One env var (`MEMTRACE_TELEMETRY_DISABLED=1`) is the kill switch.

For the formal versions, see [`PRIVACY.md`](../PRIVACY.md) and
[`TELEMETRY.md`](../TELEMETRY.md).

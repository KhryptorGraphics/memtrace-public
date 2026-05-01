# Contributing to Memtrace

Thank you for your interest in contributing to Memtrace — the missing memory layer for coding agents!

## Getting Started

1. **Fork** the repository and clone it locally.
2. Install Memtrace globally: `npm install -g memtrace`
3. Explore the codebase and try running Memtrace against your own projects.

## Ways to Contribute

- **Bug reports:** Open an issue with clear reproduction steps and your environment details.
- **Feature requests:** Open an issue describing the use case and expected behavior.
- **Documentation:** Improve the README, add examples, or clarify setup flows for Claude, Cursor, VS Code MCP, or Windsurf.
- **Benchmarks:** Add new corpora or extend the harness under `benchmarks/` — see `benchmarks/README.md`.
- **MCP Skills:** Refine or add new workflow skills under `plugins/memtrace-skills/`.
- **Bug fixes & features:** Submit a focused PR targeting the `main` branch.

## Pull Request Guidelines

- Keep PRs narrowly scoped (one concern per PR).
- Describe the motivation and what changed in the PR description.
- Run any existing benchmark or build scripts before submitting and mention results.
- Any changes touching data collection must respect the guarantees in `PRIVACY.md` and `TELEMETRY.md`, and must honor `MEMTRACE_TELEMETRY=off`.

## Code of Conduct

Be respectful and constructive. We are building developer tooling for the AI-native era — collaboration and good faith are essential.

## Questions?

Open a GitHub Discussion or join our community via the links in the README.

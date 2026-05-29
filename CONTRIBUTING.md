# Contributing to Fulcrum Gateway

Thanks for improving Fulcrum Gateway / `axctl`. This repository is public-facing, so
changes should be easy for operators to understand, test, and release.

## Code of Conduct

This project is governed by our [Code of Conduct](./CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it. Report unacceptable behavior to
**support@fulcrumdefense.ai**.

## New Here?

If you are joining the project for the first time:

1. Read through this guide and the [Development Setup](#development-setup) section below
2. Browse **[Good First Issues](https://github.com/FulcrumDefense/fulcrum-gateway/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)** —
   small, well-scoped tasks tagged for newcomers

## Getting Started

### Prerequisites

- **Python 3.11+**
- **Git**

### Fork & Clone

1. Fork this repository on GitHub
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/fulcrum-gateway.git
   cd ax-gateway
   ```
3. Add upstream remote:
   ```bash
   git remote add upstream https://github.com/FulcrumDefense/fulcrum-gateway.git
   ```

## Development Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v --tb=short
ruff check ax_cli/
ruff format --check ax_cli/
python -m build
```

Use `pipx install axctl` for normal CLI use. Use editable installs only for
local development.

## Branching

Trunk-based development. Everything branches off `main`, merges back to `main`. No long-lived branches.

```
main (protected)
  ├── feat/FUL-42/credential-unification
  ├── fix/FUL-55/retry-storm-backoff
  ├── docs/scenario-rotate-pat
  └── chore/ruff-format-precommit
```

Branch naming: `<type>/<jira-id>/<short-description>` when there is a Jira story, otherwise `<type>/<short-description>`. Types: `feat`, `fix`, `docs`, `chore`, `test`, `ci`.

Feature branches should stay under a week old. If something ages past 5 days, rebase or split it.

**Note:** `dev/staging` is dormant as of 2026-05-07 and far behind `main`. Do not branch from it - PRs cut from `dev/staging` will silently revert recent work.

### Keeping Your Fork in Sync

If you are working from a fork, pull upstream changes before starting new work:

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

Same goes for upstream dependencies like hermes-agent - pull fresh before starting work.

## Commit Style

Use Conventional Commits so Release Please can generate the changelog and
version bump correctly:

- `fix:` for compatible bug fixes
- `feat:` for user-visible CLI capability
- `docs:`, `test:`, `ci:`, `chore:`, and `style:` for non-release metadata
- Use `!` or a `BREAKING CHANGE:` footer only when the operator-facing contract
  changes incompatibly

## Security and Credentials

`axctl` handles user PATs, agent PATs, exchanged JWTs, and profile metadata.
Treat identity boundaries as part of the product contract:

- Do not log raw tokens.
- Do not use user PATs as long-running agent credentials.
- Agent-authored sends should use agent-bound credentials.
- User PATs are bootstrap credentials used to establish trust and mint scoped
  credentials.
- Update tests and docs for any token, profile, JWT, or identity behavior
  change.

## Pull Request Guidelines

### Before Opening a PR

1. `pytest tests/ -v --tb=short` - all green
2. `ruff check ax_cli/` - no lint errors
3. `ruff format --check ax_cli/` - no format issues
4. `python -m build && twine check dist/*` - package builds clean
5. New code has unit tests
6. Unit test coverage at 80%+ for changed files
7. Reference any issue or bug the PR closes in the commit message
8. No sensitive data committed
9. Branch is up to date with target branch

Use the PR template. One approving review minimum.

### Merge Strategy

- **Squash-and-merge** for single-commit PRs
- **Regular merge** for multi-commit PRs where the commit history tells a useful story

Delete the branch after merge.

### Before Starting New Work

Check outstanding issues and PRs for related or conflicting code. If your work touches the same files as an open PR, coordinate with the author to avoid painful rebases.

## Definition of Done

A contribution is complete when:

- New code has unit tests, 80%+ coverage on changed files
- All CI passes (tests, lint, format, build)
- PR reviewed and approved
- Merged to main
- GitHub issue closed (if applicable)
- Docs updated if the change is user-facing
- Conventional commit message for changelog

## Release Process

See [docs/release-process.md](docs/release-process.md).

The short version:

1. Branch off `main` and open a PR.
2. Merge to `main` after review and CI pass.
3. Release Please opens a release PR.
4. Merge the release PR after reviewing the version and changelog.
5. GitHub Release publication triggers PyPI publishing.

## Community & Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/FulcrumDefense/fulcrum-gateway/issues)
- **Security Vulnerabilities**: See [SECURITY.md](./SECURITY.md) — do not open a public issue

## License

By contributing to Fulcrum Gateway, you agree that your contributions will be
licensed under the **MIT License**.

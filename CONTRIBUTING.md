# Contributing to OctoForge

Thanks for taking a look. OctoForge is currently a solo-maintained project, so the process here is intentionally light. Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md) — the short version is: be respectful.

## Reporting bugs or suggesting ideas

Open an issue. For bugs, include what you expected, what actually happened, and enough to reproduce it. For ideas, a short description of the problem it solves is more useful than a full spec.

## Before opening a pull request

- For anything beyond a small, obvious fix, open an issue first to agree on the approach — it saves you from writing a PR that doesn't end up landing.
- Run the same gate CI runs, for both projects:
  ```bash
  make check   # Ruff check/format -> Pylint -> mypy strict -> docs -> pytest
  ```
  If it fails on code CI is happy with, your `.venv` is probably behind — CI resolves dependencies fresh on every run, while `make install` keeps whatever already satisfies the constraints. `make upgrade` refreshes it.
- Tests ship with the change. A bug fix without a regression test, or a feature without coverage, will come back with a request to add one.

## Code conventions

The short version lives in [CLAUDE.md](CLAUDE.md): strict typing (mypy `--strict`, no bare `Any`),
UTC-aware datetimes only, domain objects/enums instead of raw dicts, and a hard split between the
framework-free `core/` library and its adapters. Production Python modules have at most 120 lines;
functions have at most 20 statements, 3 arguments, McCabe complexity 10 and 3 boolean terms. Tests
only relax Pylint's statement budget to 40 and module length. Do not add `noqa`, per-file ignores or
baselines to bypass these budgets; split code by responsibility instead.

## Commit messages

English, [Conventional Commits](https://www.conventionalcommits.org/)-style (`feat(core): ...`, `fix(web): ...`) — run `git log` for examples from this repo.

## License

By contributing, you agree that your contributions are licensed under the project's
[Business Source License 1.1](LICENSE), and you grant the Licensor
([Dmitry Prokofyev](https://github.com/dmirain)) the right to relicense them, including
under the Change License (AGPL-3.0-only) and commercial licenses.

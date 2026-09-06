# Contributing

Thanks for taking the time. This project favours small, well-tested changes with an explicit rationale.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
make dev
cp .env.example .env
make seed
make run
```

## Before opening a pull request

```bash
make check    # ruff, mypy, bandit, pytest
```

Every change needs a test. Bug fixes need a test that fails before the fix.

## Standards

- Python 3.11+, line length 100, ruff for lint and format.
- Type hints on public functions; `from __future__ import annotations` at the top of modules.
- No new runtime dependency without a note in the PR describing why the standard library or an existing dependency will not do.
- Never commit `.env`, database files, model artifacts, or real staff data. Use `scripts/seed_demo.py`.

## Domain-specific rules

**Forecasting.** Any new feature must be computable at planning time. Anything derived from the target at or after the predicted timestamp is leakage and will be rejected. Splits are by date, never random.

**Solver.** New constraints go in `app/optimizer/constraints.py` and must be enforced in both `milp.py` and `greedy.py`, with a parametrised test covering both. Hard constraints belong in the model or in variable construction; preferences belong in the objective with a penalty weight.

**Models and solvers.** Register in `MODEL_REGISTRY` or `SOLVER_REGISTRY`. If extension requires editing more than that one dict, the abstraction needs fixing first.

**Security.** Do not silence a bandit finding with `# nosec`. Change the code so the finding no longer applies. Auth or token changes need a test demonstrating the attack is blocked.

## Commit messages

Conventional commits: `feat:`, `fix:`, `perf:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open a public issue for a vulnerability.

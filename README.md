# Dynamic Restaurant Workforce Optimization

Forecast hourly guest demand, translate it into per-role headcount targets, and solve for the cheapest legal shift roster that covers it.

FastAPI backend · scikit-learn demand model · MILP scheduler (PuLP/CBC) with a greedy fallback · zero-build dashboard · Argon2id + rotating-JWT auth · Docker · CI with lint, types, tests, SAST, dependency CVEs, secret scan, and image scan.

![python](https://img.shields.io/badge/python-3.11%20|%203.12-blue)
![tests](https://img.shields.io/badge/tests-54%20passing-brightgreen)
![coverage](https://img.shields.io/badge/coverage-94%25-brightgreen)
![license](https://img.shields.io/badge/license-MIT-black)

---

## The problem

A restaurant manager builds next week's rota by hand. Demand varies by weekday, by hour, by weather, by holiday. Overstaff and margin evaporates; understaff and service collapses. Meanwhile every roster must respect availability, approved time off, weekly hour caps, minimum rest between shifts, consecutive-day limits, and role qualification.

This system does the whole loop end to end:

```
sales history  ->  demand model  ->  headcount targets  ->  MILP  ->  roster + cost report
   (hourly)        (hist_gbr)         (per role/hour)      (CBC)      (publishable)
```

## Results on the bundled demo dataset

180 days of synthetic hourly covers, 28 staff, 9 shift templates, one-week horizon:

| Metric | Value |
|---|---|
| Forecast MAE | 4.76 covers/hour |
| Forecast MAPE | 9.27 % |
| Forecast R² | 0.958 |
| MILP solve time | 1.6 s (369 binaries, 1221 constraints) |
| Solver status | optimal |
| Rest/hour-cap/qualification violations | 0 (asserted in tests) |
| Test suite | 54 passing, 94 % line coverage |

Reproduce with `make seed && make run`, then press **Build schedule** in the dashboard.

---

## Quick start

```bash
git clone https://github.com/zulkarnine/restaurant-workforce-optimizer.git
cd restaurant-workforce-optimizer

python -m venv .venv && source .venv/bin/activate
make dev                 # editable install + dev tooling
cp .env.example .env     # then edit RWO_SECRET_KEY and RWO_ADMIN_PASSWORD

make seed                # 180 days of demand + 28 employees into SQLite
make run                 # http://localhost:8000
```

Open <http://localhost:8000> and sign in with the credentials from `.env`. API reference at `/docs` (disabled automatically when `RWO_ENV=prod`).

Docker instead:

```bash
docker compose up --build      # API on :8000, Postgres on :5432
```

Codespaces: open the repo in a codespace; `.devcontainer` installs everything and seeds demo data on create.

---

## How the pieces work

### 1. Demand model (`app/ml/`)

Target is covers per service hour. Features are cyclic hour and weekday encodings, weekend and holiday flags, a weather score, `lag_168` and `lag_336` (same hour one and two weeks back), a four-week same-slot rolling mean, and a four-week day-total rolling mean.

Every feature at time *t* reads only data at least 168 hours older than *t*, so there is no target leakage. The first 14 days of any history are dropped as lag warmup. The holdout split is by date, never random — a random split on time series leaks the future into training.

Models are registered in one dict:

```python
MODEL_REGISTRY = {"ridge": _ridge, "random_forest": _random_forest, "hist_gbr": _hist_gbr}
```

Adding a model means adding one entry — the API, dashboard dropdown, and validation all read from that dict. Under 120 usable rows the forecaster degrades to seasonal-naive (mean of the same weekday-hour over the trailing four weeks) and reports `fallback: true` rather than fitting a model on noise. Horizons beyond one week are predicted recursively: each prediction becomes a lag for later slots.

### 2. Headcount translation (`app/optimizer/constraints.py`)

```python
ROLE_POLICY = {
    "server":    {"covers_per_staff": 12, "min_on_duty": 1, "max_on_duty": 12},
    "cook":      {"covers_per_staff": 18, "min_on_duty": 1, "max_on_duty": 8},
    ...
}
required = clamp(ceil(covers / covers_per_staff), min_on_duty, max_on_duty)
```

Productivity ratios are the one place operational reality is encoded, kept separate from the solver so a manager can tune them without touching the model.

### 3. Scheduler (`app/optimizer/milp.py`)

Decision variable `x[e,d,t] ∈ {0,1}` — employee *e* works template *t* on day *d*. Variables are only created for feasible triples, so role qualification, availability windows, and approved time off never enter the model as constraints; they shrink it.

Objective:

```
min  wage cost
   + overtime premium (0.5 × rate on hours past 40/week)
   + understaff_penalty × missing staff-hours
   + overstaff_penalty  × surplus staff-hours
   + min_hours_penalty  × contracted-hours shortfall
```

Hard constraints: one shift per employee per day; weekly hour cap; minimum rest across midnight (a 23:00 close followed by a 10:00 open is 11 hours — configurable, and blocked pairwise); maximum consecutive working days over a sliding window.

Coverage is soft on purpose. A hard coverage constraint makes the model infeasible the moment the roster is short-handed, which tells a manager nothing. Penalised slack instead returns the best achievable roster plus an exact staff-hour deficit — an actionable hiring signal.

If CBC is unavailable, `optimizer.solve()` catches `SolverUnavailable`, runs the greedy heuristic, and tags the result `meta.degraded_from = "milp"` instead of failing the request. A test asserts the MILP's penalty-adjusted cost never exceeds greedy's.

### 4. API and dashboard

The dashboard is three static files, no build step, no framework. Tokens live in a closure variable — never `localStorage`, so injected script cannot read them back out of persistent storage. The CSP has no `unsafe-inline`, which is why the script sits in `/static/app.js` rather than a `<script>` block.

The centrepiece is a coverage grid: one cell per service hour showing `scheduled/required`, tinted by deficit depth. A manager sees Friday 19:00 short two servers at a glance.

---

## API

All routes are under `/api/v1`. Roles rank `viewer < manager < admin`.

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login` | — | issue access + refresh pair |
| POST | `/auth/refresh` | — | rotate refresh token |
| POST | `/auth/logout` | — | revoke the token family |
| GET | `/auth/me` | viewer | current identity |
| POST | `/auth/users` | admin | create a user |
| GET/POST | `/employees` | viewer / manager | roster CRUD |
| PUT | `/employees/{id}/availability` | manager | replace weekly availability |
| POST | `/employees/{id}/time-off` | manager | approve a day off |
| GET/POST | `/shift-templates` | viewer / manager | shift definitions |
| POST | `/demand/ingest` | manager | upsert hourly sales (idempotent on day+hour) |
| GET | `/demand/history` | viewer | paged sales history |
| GET | `/models` | viewer | registered forecast models |
| POST | `/forecast/train` | manager | fit, report holdout metrics, persist artifact |
| GET | `/forecast` | viewer | hourly covers for a horizon |
| POST | `/schedules/optimize` | manager | forecast + solve + persist draft |
| GET | `/schedules/{id}` | viewer | roster with coverage report |
| POST | `/schedules/{id}/publish` | manager | draft → published |
| GET | `/schedules/{id}/labor-summary` | viewer | hours and cost by employee, role, day |
| GET | `/health`, `/ready` | — | liveness, readiness |

Example:

```bash
TOKEN=$(curl -sX POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"admin@rwo.app","password":"ChangeMe!Admin123"}' | jq -r .access_token)

curl -sX POST localhost:8000/api/v1/schedules/optimize \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"start_day":"2026-09-07","days":7,"model":"hist_gbr","solver":"milp","min_rest_hours":11}' | jq '.labor_cost, .understaffed_hours'
```

---

## Security

Full threat model in [SECURITY.md](SECURITY.md). Summary of what is implemented:

- **Argon2id** password hashing (t=3, m=64 MiB, p=2) with transparent rehash on parameter change
- **Refresh-token rotation with family revocation** — replaying a consumed token burns every token in its lineage, so a stolen refresh token yields at most one extra access token before the session dies
- **Login hardening** — decoy hash so unknown accounts cost the same as wrong passwords (no user enumeration), lockout after 8 failures, tighter rate-limit bucket on `/auth`
- **CSP without `unsafe-inline`**, nosniff, frame-ancestors none, no-referrer, Permissions-Policy, COOP, `Server` header stripped
- **Request caps** — 413 above the body limit, sliding-window per-IP rate limiting, request-id correlation on every response
- **Strict schemas** — `extra="forbid"` on every input model; unknown fields are rejected rather than ignored
- **Prod guards** — startup refuses to run with the development secret, the default admin password, or wildcard CORS; `/docs` and `/openapi.json` disappear
- **Audit log** — login, refresh replay, user creation, schedule optimise/publish, with a scrubber that drops anything resembling a credential
- **Container** — multi-stage build, non-root UID 10001, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`

Soft delete on employees is deliberate: published schedules must keep referencing historical staff.

---

## Testing

```bash
make test     # 54 tests, ~20 s
make cov      # with coverage
make check    # lint + types + bandit + tests, same as CI
```

The suite is offline and deterministic — no network, no external services, synthetic fixtures with fixed seeds, a throwaway SQLite file per test.

What is actually asserted, beyond happy paths:

- refresh replay revokes the whole family, and the rotated token dies with it
- unknown user and wrong password return byte-identical responses
- a viewer gets 403 on every write route
- no employee is double-booked in a day, under either solver
- weekly hour caps, rest windows, time off, availability windows, and role qualification hold under both solvers
- MILP is never worse than greedy once penalties are counted
- feature matrix skips lag warmup and contains no NaN
- short history triggers the seasonal-naive fallback instead of a garbage fit
- the trained model preserves the lunch peak (a metric can look fine while the shape is wrong)
- rate limiter returns 429, oversized bodies return 413, prod settings reject the dev secret and wildcard CORS

## Project layout

```
app/
  config.py            settings + production guards
  db.py                async engine, session factory
  models.py            SQLAlchemy 2.0 ORM
  schemas.py           strict Pydantic contracts
  deps.py              auth dependencies, RBAC
  security/            argon2, JWT, middleware
  ml/                  features, model registry, forecaster
  optimizer/           labour rules, MILP, greedy, dispatcher
  services/            planner orchestration, seeding, audit
  api/                 auth, workforce, demand, schedule, health
  web/                 dashboard (index.html, app.js, styles.css)
tests/                 54 tests
scripts/seed_demo.py   synthetic demand + roster generator
docs/ARCHITECTURE.md   data flow, model math, scaling notes
```

## Roadmap

- Redis-backed rate limiting and a shared solver queue for multi-replica deployment
- Alembic migrations (schema is currently created from metadata at startup)
- Employee self-service: shift swaps and availability requests with manager approval
- Multi-site scheduling with staff shared across locations
- Fairness objective: variance penalty on hours distribution and weekend rotation
- CP-SAT (OR-Tools) backend behind the same `SOLVER_REGISTRY` interface

## License

MIT — see [LICENSE](LICENSE).

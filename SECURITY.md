# Security Policy

## Reporting a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting (Security tab -> Report a vulnerability). Expect an acknowledgement within 72 hours and a fix or mitigation plan within 14 days for anything rated high or critical.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| < 1.0 | no |

## Threat model

Assets: staff personal data (names, availability, pay rates), sales history, manager credentials, published rosters.

Assumed adversaries: an unauthenticated internet attacker, a credential-stuffing bot, a low-privilege insider (viewer role) attempting privilege escalation, and an attacker with a stolen refresh token.

Out of scope: physical access to the host, a compromised administrator account, and denial of service beyond the per-instance rate limiter.

## Controls

### Authentication

- Argon2id via `argon2-cffi`, time cost 3, memory 64 MiB, parallelism 2, 32-byte hash, 16-byte salt. `check_needs_rehash` runs on every successful login, so raising the parameters upgrades hashes transparently.
- Passwords must be at least 12 characters with upper, lower, and digit. Validation happens in the schema, before any handler runs.
- Login failures increment a counter; 8 failures lock the account for 15 minutes. Both windows are configurable.
- An unknown email still performs a verification against a decoy hash, so timing and response body are identical to a wrong password. No user enumeration.

### Tokens

- Access tokens are short-lived (15 minutes default), carry `sub`, `role`, `jti`, `iss`, and a `kind` claim.
- The `kind` claim prevents token-kind confusion: a refresh token presented as a bearer credential is rejected, and a test asserts it.
- Refresh tokens rotate on every use. Each is stored by `jti` with a `family` identifier. Presenting a consumed or unknown refresh token revokes every token in that family, which terminates a session built on a stolen token.
- Logout revokes the family, so it is a real logout rather than a client-side token drop.
- Decoding requires `exp`, `iat`, `sub`, `jti` and validates the issuer, so unsigned or trimmed tokens fail closed.

### Authorisation

Three ranked roles: `viewer` (read), `manager` (write, plan, publish), `admin` (user management). Enforcement is a dependency factory applied per route, not a check inside handler bodies, so an unguarded route is visible in the signature during review.

### Transport and input

- Content-Security-Policy without `unsafe-inline` or `unsafe-eval`; `object-src 'none'`, `base-uri 'none'`, `frame-ancestors 'none'`.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy` denying geolocation, microphone, camera, `Cross-Origin-Opener-Policy: same-origin`, `Cache-Control: no-store`, `Server` header removed. HSTS is enabled automatically when `RWO_ENV=prod`.
- CORS is an explicit allowlist with credentials disabled; wildcard origins are rejected at startup in production.
- `TrustedHostMiddleware` blocks Host-header spoofing.
- Requests above `RWO_MAX_BODY_BYTES` (2 MiB default) are rejected with 413 before the body is read.
- Sliding-window per-IP rate limiting, with a tighter bucket for `/api/v1/auth`. This is per process; behind multiple replicas move it to Redis (see docs/ARCHITECTURE.md).
- Every Pydantic input model sets `extra="forbid"`, so unexpected fields are a 422 rather than silently ignored mass assignment.
- Validation errors are sanitised to `loc`, `msg`, `type`. Raw payloads and exception objects are never reflected back.
- Unhandled exceptions return a request id only; the stack trace goes to the structured log.

### Data handling

- SQL is built exclusively through SQLAlchemy expressions; no string-interpolated queries.
- The audit log scrubs any detail containing `password`, `token`, `secret`, or `authorization` before writing.
- Employees are soft-deleted so published schedules keep valid references, and historical labour cost stays auditable.
- Model artifacts contain fitted estimators only, never raw sales rows.

### Supply chain and runtime

- CI runs ruff, mypy, pytest with coverage, bandit (SAST), pip-audit (dependency CVEs), gitleaks (secret scan), and Trivy against the built image, failing on HIGH or CRITICAL.
- Dependabot watches pip, GitHub Actions, and Docker.
- The image is multi-stage, runs as non-root UID 10001, and Compose applies `read_only`, `cap_drop: ALL`, and `no-new-privileges`.

## Deployment checklist

1. `RWO_SECRET_KEY` from `python -c "import secrets;print(secrets.token_urlsafe(48))"` — startup refuses the development default in prod.
2. `RWO_ADMIN_PASSWORD` set to a real value; rotate the bootstrap admin after first login.
3. `RWO_CORS_ORIGINS` and `RWO_TRUSTED_HOSTS` narrowed to real hostnames.
4. TLS terminated upstream; run uvicorn with `--proxy-headers` and a trusted `--forwarded-allow-ips` so rate limiting and audit logs see real client IPs.
5. Postgres rather than SQLite; back it up and restrict network access to the API.
6. Ship structured logs to a collector and alert on `login_failed`, `refresh_replay`, and `refresh_reuse` volume.

## Known limitations

- Rate limiting and login lockout state are per process; a multi-replica deployment needs a shared store.
- Schema is created from metadata at startup. Production upgrades should move to Alembic before the first migration is needed.
- No MFA. It is the next authentication increment.

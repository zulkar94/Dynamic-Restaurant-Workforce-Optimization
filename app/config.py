"""Runtime configuration. All values overridable via RWO_* env vars or .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_PLACEHOLDER_KEY = "dev-insecure-change-me-0000000000000000"
DEV_PLACEHOLDER_ADMIN = "ChangeMe!Admin123"

# kept for import compatibility with older call sites and tests
DEV_SECRET = DEV_PLACEHOLDER_KEY


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="RWO_", extra="ignore", case_sensitive=False
    )

    # --- core ---
    env: str = "dev"  # dev | test | prod
    app_name: str = "Dynamic Restaurant Workforce Optimization"
    debug: bool = False
    database_url: str = "sqlite+aiosqlite:///./rwo.db"
    artifacts_dir: str = "./artifacts"

    # --- auth ---
    secret_key: str = Field(default=DEV_PLACEHOLDER_KEY, min_length=32)
    jwt_algorithm: str = "HS256"
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 14
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 65536  # KiB
    argon2_parallelism: int = 2
    max_login_failures: int = 8
    lockout_seconds: int = 900

    # --- bootstrap admin (first boot only) ---
    admin_email: str = "admin@rwo.app"
    admin_password: str = DEV_PLACEHOLDER_ADMIN

    # --- transport hardening ---
    cors_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000"]
    trusted_hosts: list[str] = ["*"]
    max_body_bytes: int = 2 * 1024 * 1024
    rate_limit_per_minute: int = 240
    auth_rate_limit_per_minute: int = 12
    enable_hsts: bool = False

    # --- domain defaults ---
    open_hour: int = 10
    close_hour: int = 23
    horizon_days_max: int = 21
    solver_time_limit_seconds: int = 30

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.env == "prod":
            if self.secret_key == DEV_PLACEHOLDER_KEY:
                raise ValueError("RWO_SECRET_KEY must be set in prod")
            if self.admin_password == DEV_PLACEHOLDER_ADMIN:
                raise ValueError("RWO_ADMIN_PASSWORD must be set in prod")
            if "*" in self.cors_origins:
                raise ValueError("wildcard CORS forbidden in prod")
            self.debug = False
            self.enable_hsts = True
        if self.close_hour <= self.open_hour:
            raise ValueError("close_hour must exceed open_hour")
        return self

    @property
    def service_hours(self) -> range:
        return range(self.open_hour, self.close_hour)


@lru_cache
def get_settings() -> Settings:
    return Settings()

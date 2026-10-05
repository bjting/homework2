"""Configuration management for PostgreSQL MCP Server.

This module defines all configuration settings using Pydantic for validation
and type safety. Configuration is loaded from environment variables with
sensible defaults.

Multi-database support:
    The primary database is configured via ``DATABASE_*`` environment
    variables. Additional databases can be declared with the
    ``EXTRA_DATABASES`` environment variable as a JSON array, e.g.::

        EXTRA_DATABASES=[{"name": "warehouse", "host": "db.internal"}]

    Extra databases inherit any connection field that is omitted from their
    JSON entry (falling back to the ``DATABASE_*`` defaults).
"""

import json
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseSettings):
    """PostgreSQL database connection configuration."""

    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="postgres", description="Database name")
    user: str = Field(default="postgres", description="Database user")
    password: str = Field(default="", description="Database password")

    # Connection pool settings
    min_pool_size: int = Field(default=5, ge=1, le=100, description="Minimum pool size")
    max_pool_size: int = Field(default=20, ge=1, le=100, description="Maximum pool size")
    pool_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Pool acquire timeout in seconds"
    )
    command_timeout: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Command execution timeout in seconds"
    )

    @property
    def dsn(self) -> str:
        """Build PostgreSQL DSN connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def safe_dsn(self) -> str:
        """Build DSN with masked password for logging."""
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.name}"


class OpenAIConfig(BaseSettings):
    """OpenAI API configuration."""

    model_config = SettingsConfigDict(env_prefix="OPENAI_")

    api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible API base URL for relay/proxy endpoints (None = official)",
    )
    model: str = Field(default="gpt-4o-mini", description="Model to use for SQL generation")
    max_tokens: int = Field(
        default=2000, ge=100, le=128000, description="Maximum tokens in response"
    )
    temperature: float = Field(
        default=0.0, ge=0.0, le=2.0, description="Temperature for response randomness"
    )
    timeout: float = Field(
        default=30.0, ge=5.0, le=120.0, description="API request timeout in seconds"
    )

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Validate API key is not empty and has correct format."""
        api_key_str = v.get_secret_value()
        if not api_key_str or not api_key_str.strip():
            raise ValueError("OpenAI API key must not be empty")
        if not api_key_str.startswith("sk-"):
            raise ValueError("OpenAI API key must start with 'sk-'")
        return v


class SecurityConfig(BaseSettings):
    """Security and access control configuration."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    allow_write_operations: bool = Field(
        default=False, description="Allow write operations (INSERT, UPDATE, DELETE)"
    )
    # NOTE: annotated as `str | list[str]` on purpose. pydantic-settings treats
    # plain list fields as "complex" and JSON-decodes env values itself, which
    # rejects the comma-separated format documented in .env.example before our
    # validator can run. A union type is not "complex", so the raw string
    # reaches parse_string_list below. After validation the value is a list.
    blocked_functions: str | list[str] = Field(
        default_factory=lambda: [
            "pg_sleep",
            "pg_read_file",
            "pg_write_file",
            "lo_import",
            "lo_export",
        ],
        description="List of blocked PostgreSQL functions",
    )
    blocked_tables: str | list[str] = Field(
        default_factory=list,
        description="Tables that queries may not access (sensitive objects)",
    )
    blocked_columns: str | list[str] = Field(
        default_factory=list,
        description="Columns that queries may not access (accepts 'column' or 'table.column')",
    )
    allow_explain: bool = Field(
        default=False,
        description="Allow EXPLAIN statements (read-only query plan inspection)",
    )
    max_rows: int = Field(default=10000, ge=1, le=100000, description="Maximum rows to return")
    max_execution_time: float = Field(
        default=30.0, ge=1.0, le=300.0, description="Maximum query execution time in seconds"
    )
    readonly_role: str | None = Field(
        default=None, description="PostgreSQL role to switch to for read-only access"
    )
    safe_search_path: str = Field(
        default="public", description="Safe search_path to set during query execution"
    )

    @field_validator("blocked_functions", "blocked_tables", "blocked_columns", mode="before")
    @classmethod
    def parse_string_list(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list."""
        if isinstance(v, str):
            return [f.strip() for f in v.split(",") if f.strip()]
        return v

    @property
    def access_policy_prompt(self) -> str | None:
        """Blocked-objects description injected into the SQL generation prompt.

        Tells the LLM which objects are off-limits and instructs it to answer
        ``ACCESS_DENIED: <object>`` instead of substituting other tables, so
        users get an explicit permission message rather than silently
        different data.

        Returns:
            str | None: Policy text, or None when nothing is blocked.
        """
        if not self.blocked_tables and not self.blocked_columns:
            return None
        lines: list[str] = []
        if self.blocked_tables:
            lines.append("Blocked tables (absolute access forbidden):")
            lines.extend(f"- {t}" for t in self.blocked_tables)
        if self.blocked_columns:
            lines.append("Blocked columns (must NOT appear anywhere in any query):")
            lines.extend(f"- {c}" for c in self.blocked_columns)
        lines.append(
            "If the user's question requires a blocked table or column, do NOT "
            "substitute a different table or column. Reply with exactly one line "
            "and nothing else:\nACCESS_DENIED: <blocked object name>"
        )
        return "\n".join(lines)


class ValidationConfig(BaseSettings):
    """Query validation configuration."""

    model_config = SettingsConfigDict(env_prefix="VALIDATION_")

    max_question_length: int = Field(
        default=10000, ge=1, le=50000, description="Maximum question length in characters"
    )

    # Result validation settings
    enabled: bool = Field(default=True, description="Enable result validation using LLM")
    sample_rows: int = Field(
        default=5, ge=1, le=100, description="Number of sample rows to include in validation"
    )
    timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, description="Result validation timeout in seconds"
    )
    confidence_threshold: int = Field(
        default=70, ge=0, le=100, description="Minimum confidence for acceptable results"
    )


class CacheConfig(BaseSettings):
    """Schema cache configuration."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    schema_ttl: int = Field(
        default=3600, ge=60, le=86400, description="Schema cache TTL in seconds"
    )
    max_size: int = Field(default=100, ge=1, le=1000, description="Maximum cache entries")
    enabled: bool = Field(default=True, description="Enable schema caching")


class ResilienceConfig(BaseSettings):
    """Resilience and fault tolerance configuration."""

    model_config = SettingsConfigDict(env_prefix="RESILIENCE_")

    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum retry attempts")
    retry_delay: float = Field(
        default=1.0, ge=0.1, le=10.0, description="Initial retry delay in seconds"
    )
    backoff_factor: float = Field(
        default=2.0, ge=1.0, le=10.0, description="Exponential backoff factor"
    )
    circuit_breaker_threshold: int = Field(
        default=5, ge=1, le=100, description="Failures before circuit opens"
    )
    circuit_breaker_timeout: float = Field(
        default=60.0, ge=10.0, le=300.0, description="Circuit breaker timeout in seconds"
    )

    # Concurrency limits (enforced by MultiRateLimiter in the request path)
    query_rate_limit: int = Field(
        default=10, ge=1, le=1000, description="Max concurrent query requests"
    )
    llm_rate_limit: int = Field(
        default=5, ge=1, le=1000, description="Max concurrent LLM API calls"
    )
    rate_limit_timeout: float = Field(
        default=5.0, ge=0.1, le=60.0, description="Seconds to wait for a rate limiter slot"
    )


class ObservabilityConfig(BaseSettings):
    """Observability and monitoring configuration."""

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_")

    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics")
    metrics_port: int = Field(
        default=9090, ge=1024, le=65535, description="Metrics HTTP server port"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Logging level"
    )
    log_format: Literal["json", "text"] = Field(default="text", description="Log format")


class Settings(BaseSettings):
    """Main application settings aggregating all config sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Application environment"
    )

    # Nested configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    extra_databases: list[DatabaseConfig] = Field(
        default_factory=list,
        description="Additional databases (JSON array via EXTRA_DATABASES env var)",
    )
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    @field_validator("extra_databases", mode="before")
    @classmethod
    def parse_extra_databases(cls, v: str | list[dict] | list[DatabaseConfig] | None):
        """Parse EXTRA_DATABASES from a JSON string when provided via env var.

        Args:
            v: JSON string, list of dicts, list of DatabaseConfig, or None.

        Returns:
            list[DatabaseConfig]: Parsed extra database configurations.
        """
        if v is None or v == "":
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError as e:
                raise ValueError(f"EXTRA_DATABASES is not valid JSON: {e}") from e
            if not isinstance(parsed, list):
                raise ValueError("EXTRA_DATABASES must be a JSON array of database objects")
            return parsed
        return v

    @model_validator(mode="after")
    def validate_databases(self) -> "Settings":
        """Ensure every configured database has a unique name."""
        names = [self.database.name] + [db.name for db in self.extra_databases]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Duplicate database names configured: {sorted(duplicates)}")
        return self

    @property
    def databases(self) -> dict[str, DatabaseConfig]:
        """All configured databases keyed by name (primary first)."""
        configs = [self.database, *self.extra_databases]
        return {config.name: config for config in configs}

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create global settings instance.

    Returns:
        Settings: The global settings instance.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset global settings instance. Useful for testing."""
    global _settings
    _settings = None

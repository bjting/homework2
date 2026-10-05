"""Unit tests for QueryOrchestrator.

This module tests the orchestrator's coordination of the query pipeline,
including multi-database executor routing, retry logic with exponential
backoff, rate limiting, observability integration, and error handling.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    LLMError,
    LLMTimeoutError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    QueryRequest,
    ResultValidationResult,
    ReturnType,
)
from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.resilience.circuit_breaker import CircuitState
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator

FAST_RESILIENCE = {"max_retries": 3, "retry_delay": 0.1}


def make_orchestrator(
    pools: dict[str, MagicMock] | None = None,
    generator: MagicMock | None = None,
    validator: MagicMock | None = None,
    executors: dict | None = None,
    result_validator: MagicMock | None = None,
    schema_cache: MagicMock | None = None,
    resilience: ResilienceConfig | None = None,
    validation: ValidationConfig | None = None,
    metrics: MagicMock | None = None,
    rate_limiter: MultiRateLimiter | None = None,
) -> QueryOrchestrator:
    """Build a QueryOrchestrator with mocked components."""
    if pools is None:
        pools = {"test_db": MagicMock()}
    if executors is None:
        executors = {name: MagicMock() for name in pools}
    return QueryOrchestrator(
        sql_generator=generator or MagicMock(),
        sql_validator=validator or MagicMock(),
        sql_executors=executors,
        result_validator=result_validator or MagicMock(),
        schema_cache=schema_cache or MagicMock(),
        pools=pools,
        resilience_config=resilience or ResilienceConfig(**FAST_RESILIENCE),
        validation_config=validation or ValidationConfig(),
        metrics=metrics,
        rate_limiter=rate_limiter,
    )


def make_schema(db_name: str = "test_db") -> DatabaseSchema:
    """Create a mock database schema."""
    return DatabaseSchema(
        database_name=db_name,
        tables=[
            TableInfo(
                schema_name="public",
                table_name="users",
                columns=[
                    ColumnInfo(
                        name="id",
                        data_type="integer",
                        is_nullable=False,
                        is_primary_key=True,
                    ),
                    ColumnInfo(
                        name="name",
                        data_type="varchar(255)",
                        is_nullable=False,
                    ),
                ],
            )
        ],
        version="15.0",
    )


class TestDatabaseResolution:
    """Test database name resolution logic."""

    def test_resolve_database_specified_valid(self) -> None:
        """Test resolving a specified valid database."""
        orchestrator = make_orchestrator(pools={"db1": MagicMock(), "db2": MagicMock()})
        result = orchestrator._resolve_database("db1")
        assert result == "db1"

    def test_resolve_database_specified_invalid(self) -> None:
        """Test resolving a specified but invalid database."""
        orchestrator = make_orchestrator(pools={"db1": MagicMock(), "db2": MagicMock()})
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database("nonexistent")

        assert "not found" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]
        assert "db2" in exc_info.value.details["available_databases"]

    def test_resolve_database_auto_select_single(self) -> None:
        """Test auto-selecting when only one database available."""
        orchestrator = make_orchestrator(pools={"only_db": MagicMock()})
        result = orchestrator._resolve_database(None)
        assert result == "only_db"

    def test_resolve_database_auto_select_multiple_fails(self) -> None:
        """Test that auto-select fails when multiple databases available."""
        orchestrator = make_orchestrator(pools={"db1": MagicMock(), "db2": MagicMock()})
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "multiple databases" in str(exc_info.value).lower()
        assert "db1" in exc_info.value.details["available_databases"]

    def test_resolve_database_no_databases(self) -> None:
        """Test error when no databases configured."""
        orchestrator = make_orchestrator(pools={})
        with pytest.raises(DatabaseError) as exc_info:
            orchestrator._resolve_database(None)

        assert "no databases configured" in str(exc_info.value).lower()


class TestMultiDatabaseRouting:
    """Test routing requests to the executor of the resolved database."""

    @pytest.mark.asyncio
    async def test_request_routes_to_named_database_executor(self) -> None:
        """A request naming db2 must execute on db2's executor, not the primary."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema("db2")

        generator = AsyncMock()
        generator.generate.return_value = "SELECT * FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor_db1 = AsyncMock()
        executor_db2 = AsyncMock()
        executor_db2.execute.return_value = ([{"id": 1}], 1)

        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="ok",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            pools={"db1": MagicMock(), "db2": MagicMock()},
            generator=generator,
            validator=validator,
            executors={"db1": executor_db1, "db2": executor_db2},
            result_validator=result_validator,
            schema_cache=schema_cache,
        )

        request = QueryRequest(question="Get users", database="db2")
        response = await orchestrator.execute_query(request)

        # The db2 executor ran; the primary db1 executor never did
        assert response.success is True
        executor_db2.execute.assert_awaited_once_with("SELECT * FROM users;")
        executor_db1.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_executor_returns_database_error(self) -> None:
        """A pool without a matching executor yields a database_error response."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema("db2")

        orchestrator = make_orchestrator(
            pools={"db1": MagicMock(), "db2": MagicMock()},
            executors={"db1": AsyncMock()},  # db2 has pool but no executor
            schema_cache=schema_cache,
        )

        request = QueryRequest(question="Get users", database="db2")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        assert "executor" in response.error.message.lower()


class TestSQLGenerationWithRetry:
    """Test SQL generation with retry logic."""

    @pytest.mark.asyncio
    async def test_generate_sql_success_first_attempt(self) -> None:
        """Test successful SQL generation on first attempt."""
        generator = AsyncMock()
        generator.generate.return_value = "SELECT * FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None  # No exception = valid

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
        )

        sql, validation_result, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=make_schema(),
            request_id="test-123",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        generator.generate.assert_awaited_once()
        validator.validate_or_raise.assert_called_once()
        assert validator.validate_or_raise.call_args.args[0] == "SELECT * FROM users;"

    @pytest.mark.asyncio
    async def test_generate_sql_retry_on_validation_failure(self) -> None:
        """Test retry logic when validation fails."""
        generator = AsyncMock()
        generator.generate.side_effect = [
            "SELECT * FROM user;",  # First attempt (wrong table name)
            "SELECT * FROM users;",  # Second attempt (correct)
        ]

        validator = MagicMock()
        validator.validate_or_raise.side_effect = [
            SQLParseError('relation "user" does not exist'),
            None,  # Success on second attempt
        ]

        orchestrator = make_orchestrator(generator=generator, validator=validator)

        sql, validation_result, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get all users",
            schema=make_schema(),
            request_id="test-123",
        )

        assert sql == "SELECT * FROM users;"
        assert validation_result.is_valid is True
        assert generator.generate.call_count == 2
        assert validator.validate_or_raise.call_count == 2

        # Verify retry included error feedback
        second_call = generator.generate.call_args_list[1]
        assert second_call.kwargs["previous_attempt"] == "SELECT * FROM user;"
        assert 'relation "user" does not exist' in second_call.kwargs["error_feedback"]

    @pytest.mark.asyncio
    async def test_generate_sql_fails_after_max_retries(self) -> None:
        """Test failure after exhausting all retries."""
        generator = AsyncMock()
        generator.generate.return_value = "DELETE FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError(
            "DELETE statements are not allowed"
        )

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1),
        )

        with pytest.raises(SecurityViolationError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Delete all users",
                schema=make_schema(),
                request_id="test-123",
            )

        assert "DELETE statements are not allowed" in str(exc_info.value)
        # Should attempt max_retries + 1 times (initial + retries)
        assert generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_generate_sql_circuit_breaker_open(self) -> None:
        """Test that open circuit breaker prevents SQL generation."""
        orchestrator = make_orchestrator(
            generator=AsyncMock(),
            resilience=ResilienceConfig(circuit_breaker_threshold=1),
        )

        # Manually open the circuit breaker
        orchestrator.circuit_breaker._state = CircuitState.OPEN
        orchestrator.circuit_breaker._failure_count = 5

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=make_schema(),
                request_id="test-123",
            )

        assert "temporarily unavailable" in str(exc_info.value).lower()
        assert "circuit breaker" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_generate_sql_unexpected_error(self) -> None:
        """Test handling of unexpected errors during generation."""
        generator = AsyncMock()
        generator.generate.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(
            generator=generator,
            resilience=ResilienceConfig(max_retries=1, retry_delay=0.1),
        )

        with pytest.raises(LLMError) as exc_info:
            await orchestrator._generate_sql_with_retry(
                question="Get all users",
                schema=make_schema(),
                request_id="test-123",
            )

        assert "unexpectedly" in str(exc_info.value).lower()
        assert orchestrator.circuit_breaker.failure_count == 1


class TestBackoffRetry:
    """Test retry with exponential backoff for transient LLM failures."""

    @pytest.mark.asyncio
    async def test_transient_llm_timeout_retried_with_backoff(self) -> None:
        """LLM timeouts are retried with exponential backoff until success."""
        generator = AsyncMock()
        generator.generate.side_effect = [
            LLMTimeoutError("OpenAI API request timed out"),  # attempt 1
            LLMTimeoutError("OpenAI API request timed out"),  # attempt 2
            "SELECT 1;",  # attempt 3 succeeds
        ]

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1, backoff_factor=2.0),
        )

        sql, validation, _tokens = await orchestrator._generate_sql_with_retry(
            question="Get users",
            schema=make_schema(),
            request_id="test-123",
        )

        assert sql == "SELECT 1;"
        assert generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 0
        assert validation.is_valid is True

    @pytest.mark.asyncio
    async def test_transient_llm_timeout_exhausts_retries(self) -> None:
        """When all attempts time out, the timeout error is raised and recorded."""
        generator = AsyncMock()
        generator.generate.side_effect = LLMTimeoutError("OpenAI API request timed out")

        orchestrator = make_orchestrator(
            generator=generator,
            resilience=ResilienceConfig(max_retries=2, retry_delay=0.1),
        )

        with pytest.raises(LLMTimeoutError):
            await orchestrator._generate_sql_with_retry(
                question="Get users",
                schema=make_schema(),
                request_id="test-123",
            )

        assert generator.generate.call_count == 3
        assert orchestrator.circuit_breaker.failure_count == 1

    @pytest.mark.asyncio
    async def test_non_transient_llm_error_not_retried(self) -> None:
        """Authentication-style LLM errors fail fast without retries."""
        generator = AsyncMock()
        generator.generate.side_effect = LLMError("OpenAI API authentication failed")

        orchestrator = make_orchestrator(
            generator=generator,
            resilience=ResilienceConfig(max_retries=3, retry_delay=0.1),
        )

        with pytest.raises(LLMError):
            await orchestrator._generate_sql_with_retry(
                question="Get users",
                schema=make_schema(),
                request_id="test-123",
            )

        assert generator.generate.call_count == 1


class TestRateLimiting:
    """Test rate limiting integration in the request path."""

    @pytest.mark.asyncio
    async def test_query_rate_limit_rejection(self) -> None:
        """When the query limiter is saturated, requests get rate_limit_exceeded."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)
        # Saturate the query limiter
        await rate_limiter.query_limiter.acquire()

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        orchestrator = make_orchestrator(
            pools={"test_db": MagicMock()},
            schema_cache=schema_cache,
            resilience=ResilienceConfig(rate_limit_timeout=0.1),
            rate_limiter=rate_limiter,
        )

        request = QueryRequest(question="Get users", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "rate_limit_exceeded"
        # Slot was released correctly (no leak) after the failed request
        assert rate_limiter.query_limiter.active_count == 1  # Only our manual slot
        rate_limiter.query_limiter.release()

    @pytest.mark.asyncio
    async def test_query_slot_released_after_success(self) -> None:
        """The query rate-limiter slot is released after a successful request."""
        rate_limiter = MultiRateLimiter(query_limit=1, llm_limit=1)

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"
        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
            rate_limiter=rate_limiter,
        )

        request = QueryRequest(question="Ping", database="test_db", return_type=ReturnType.SQL)
        response = await orchestrator.execute_query(request)

        assert response.success is True
        # RateLimiter.release() decrements its counter in a scheduled task;
        # yield to the event loop so the decrement runs before asserting.
        await asyncio.sleep(0)
        assert rate_limiter.query_limiter.active_count == 0
        assert rate_limiter.query_limiter.get_stats()["total_requests"] == 1

    def test_default_rate_limiter_created_from_config(self) -> None:
        """Without an explicit limiter, one is built from ResilienceConfig."""
        orchestrator = make_orchestrator(
            resilience=ResilienceConfig(query_rate_limit=7, llm_rate_limit=3),
        )
        assert orchestrator.rate_limiter.query_limiter.max_concurrent == 7
        assert orchestrator.rate_limiter.llm_limiter.max_concurrent == 3


class TestObservabilityIntegration:
    """Test metrics collection and request tracing integration."""

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_success(self) -> None:
        """Successful requests record request, DB, and LLM metrics."""
        metrics = MagicMock()

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"
        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
            metrics=metrics,
        )

        request = QueryRequest(question="Ping", database="test_db", return_type=ReturnType.SQL)
        response = await orchestrator.execute_query(request)

        assert response.success is True
        metrics.increment_query_request.assert_called_once_with(
            status="success", database="test_db"
        )
        metrics.increment_llm_call.assert_called_with("generate_sql")
        metrics.observe_llm_latency.assert_called()
        metrics.query_duration.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_metrics_record_security_rejection(self) -> None:
        """Security violations record a sql_rejected metric with the reason."""
        metrics = MagicMock()

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "DELETE FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
            resilience=ResilienceConfig(max_retries=1, retry_delay=0.1),
            metrics=metrics,
        )

        request = QueryRequest(question="Delete users", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is False
        metrics.increment_sql_rejected.assert_called_with("security_violation")
        metrics.increment_query_request.assert_called_once_with(
            status="security_violation", database="test_db"
        )

    @pytest.mark.asyncio
    async def test_metrics_record_db_query_duration(self) -> None:
        """Result flows record the database execution duration."""
        metrics = MagicMock()

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"
        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor = AsyncMock()
        executor.execute.return_value = ([{"id": 1}], 1)

        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=90, explanation="ok", suggestion=None, is_acceptable=True
        )

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            executors={"test_db": executor},
            result_validator=result_validator,
            schema_cache=schema_cache,
            metrics=metrics,
        )

        request = QueryRequest(question="Ping", database="test_db")
        response = await orchestrator.execute_query(request)

        assert response.success is True
        metrics.observe_db_query_duration.assert_called_once()

    @pytest.mark.asyncio
    async def test_request_id_propagated_via_trace_context(self) -> None:
        """The tracing context carries the same request_id as the response."""
        seen_request_ids = []

        async def capture_request_id(*args, **kwargs):
            seen_request_ids.append(get_request_id())
            return "SELECT 1;"

        generator = AsyncMock()
        generator.generate.side_effect = capture_request_id

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
        )

        request = QueryRequest(question="Ping", database="test_db", return_type=ReturnType.SQL)
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.request_id is not None
        assert seen_request_ids == [response.request_id]
        # Context is cleaned up after the request completes
        assert get_request_id() is None


class TestResultValidation:
    """Test result validation logic."""

    @pytest.mark.asyncio
    async def test_validate_results_success(self) -> None:
        """Test successful result validation."""
        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=85,
            explanation="Results match the question well",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=True),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 85
        result_validator.validate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_validate_results_disabled(self) -> None:
        """Test that validation is skipped when disabled."""
        result_validator = AsyncMock()

        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=False),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100
        result_validator.validate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_validate_results_failure_does_not_raise(self) -> None:
        """Test that validation failures don't raise exceptions."""
        result_validator = AsyncMock()
        result_validator.validate.side_effect = Exception("Validation failed")

        orchestrator = make_orchestrator(
            result_validator=result_validator,
            validation=ValidationConfig(enabled=True),
        )

        confidence = await orchestrator._validate_results_safely(
            question="Count users",
            sql="SELECT COUNT(*) FROM users",
            results=[{"count": 42}],
            row_count=1,
            request_id="test-123",
        )

        assert confidence == 100


class TestExecuteQueryFlow:
    """Test complete query execution flow."""

    @pytest.mark.asyncio
    async def test_execute_query_sql_only(self) -> None:
        """Test executing query with return_type=SQL."""
        generator = AsyncMock()
        generator.generate.return_value = "SELECT * FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.generated_sql == "SELECT * FROM users;"
        assert response.validation is not None
        assert response.validation.is_valid is True
        assert response.data is None  # No execution for SQL-only
        assert response.error is None

    @pytest.mark.asyncio
    async def test_execute_query_with_results(self) -> None:
        """Test executing query with return_type=RESULT."""
        generator = AsyncMock()
        generator.generate.return_value = "SELECT id, name FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor = AsyncMock()
        executor.execute.return_value = (
            [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            2,  # total count
        )

        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="Good results",
            suggestion=None,
            is_acceptable=True,
        )

        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            executors={"test_db": executor},
            result_validator=result_validator,
            schema_cache=schema_cache,
            validation=ValidationConfig(enabled=True),
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        assert response.generated_sql == "SELECT id, name FROM users;"
        assert response.data is not None
        assert response.data.row_count == 2
        assert len(response.data.rows) == 2
        assert response.data.columns == ["id", "name"]
        assert response.confidence == 90
        assert response.error is None

    @pytest.mark.asyncio
    async def test_execute_query_schema_not_cached(self) -> None:
        """Test loading schema when not in cache."""
        schema = DatabaseSchema(database_name="test_db", tables=[], version="15.0")

        schema_cache = MagicMock()
        schema_cache.get.return_value = None  # Not in cache
        schema_cache.load = AsyncMock(return_value=schema)

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        pool = MagicMock()

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            pools={"test_db": pool},
            schema_cache=schema_cache,
        )

        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        schema_cache.load.assert_awaited_once_with("test_db", pool)
        assert response.success is True

    @pytest.mark.asyncio
    async def test_execute_query_schema_load_fails(self) -> None:
        """Test handling of schema load failure."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = None
        schema_cache.load = AsyncMock(side_effect=Exception("DB connection failed"))

        orchestrator = make_orchestrator(schema_cache=schema_cache)

        request = QueryRequest(
            question="Test query",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "schema" in response.error.message.lower()
        assert response.generated_sql is None

    @pytest.mark.asyncio
    async def test_execute_query_validation_error(self) -> None:
        """Test handling of SQL validation errors."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "DELETE FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.side_effect = SecurityViolationError("DELETE not allowed")

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
            resilience=ResilienceConfig(max_retries=1, retry_delay=0.1),
        )

        request = QueryRequest(
            question="Delete all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "DELETE not allowed" in response.error.message
        assert response.error.code == "security_violation"

    @pytest.mark.asyncio
    async def test_execute_query_execution_error(self) -> None:
        """Test handling of SQL execution errors."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "SELECT * FROM users;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError("Query execution failed")

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            executors={"test_db": executor},
            schema_cache=schema_cache,
        )

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.RESULT,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert "execution failed" in response.error.message.lower()
        assert response.error.code == "database_error"

    @pytest.mark.asyncio
    async def test_execute_query_unexpected_error(self) -> None:
        """Test handling of unexpected errors."""
        schema_cache = MagicMock()
        schema_cache.get.side_effect = RuntimeError("Unexpected error")

        orchestrator = make_orchestrator(schema_cache=schema_cache)

        request = QueryRequest(
            question="Get all users",
            database="test_db",
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "internal_error"
        assert "internal server error" in response.error.message.lower()

    @pytest.mark.asyncio
    async def test_execute_query_auto_select_database(self) -> None:
        """Test auto-selecting database when only one available."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema()

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        orchestrator = make_orchestrator(
            generator=generator,
            validator=validator,
            pools={"only_db": MagicMock()},  # Only one database
            schema_cache=schema_cache,
        )

        request = QueryRequest(
            question="Test query",
            database=None,  # No database specified
            return_type=ReturnType.SQL,
        )
        response = await orchestrator.execute_query(request)

        assert response.success is True
        # Verify schema was fetched for auto-selected database
        schema_cache.get.assert_called_once_with("only_db")


class TestExecutionFeedbackRetry:
    """Execution-time schema-mismatch errors loop back to the LLM (CRM bug)."""

    @pytest.mark.asyncio
    async def test_hallucinated_table_retries_with_feedback(self) -> None:
        """A hallucinated table name is regenerated using the DB error feedback."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema("crm_db")

        generator = AsyncMock()
        generator.generate.side_effect = [
            "SELECT COUNT(*) FROM accounts;",
            "SELECT COUNT(*) AS total FROM customers;",
        ]

        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor = AsyncMock()
        executor.execute.side_effect = [
            DatabaseError(
                'relation "accounts" does not exist',
                details={"error_code": "42P01"},
            ),
            ([{"total": 5}], 1),
        ]

        result_validator = AsyncMock()
        result_validator.validate.return_value = ResultValidationResult(
            confidence=90,
            explanation="ok",
            suggestion=None,
            is_acceptable=True,
        )

        orchestrator = make_orchestrator(
            pools={"crm_db": MagicMock()},
            executors={"crm_db": executor},
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
            result_validator=result_validator,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="How many customers?", database="crm_db")
        )

        assert response.success is True
        assert executor.execute.await_count == 2
        assert generator.generate.await_count == 2
        feedback_call = generator.generate.await_args_list[1]
        assert "accounts" in feedback_call.kwargs["error_feedback"]
        assert response.generated_sql == "SELECT COUNT(*) AS total FROM customers;"

    @pytest.mark.asyncio
    async def test_non_retryable_db_error_fails_fast(self) -> None:
        """Non-schema DB errors (e.g. connection loss) are not retried."""
        schema_cache = MagicMock()
        schema_cache.get.return_value = make_schema("db")

        generator = AsyncMock()
        generator.generate.return_value = "SELECT 1;"
        validator = MagicMock()
        validator.validate_or_raise.return_value = None

        executor = AsyncMock()
        executor.execute.side_effect = DatabaseError(
            "connection terminated", details={"error_code": "08006"}
        )

        orchestrator = make_orchestrator(
            pools={"db": MagicMock()},
            executors={"db": executor},
            generator=generator,
            validator=validator,
            schema_cache=schema_cache,
        )

        response = await orchestrator.execute_query(
            QueryRequest(question="q", database="db")
        )

        assert response.success is False
        assert response.error.code == "database_error"
        assert executor.execute.await_count == 1
        generator.generate.assert_awaited_once()

    def test_is_schema_mismatch_detection(self) -> None:
        """SQLSTATE / message based detection of retryable schema errors."""
        assert QueryOrchestrator._is_schema_mismatch_error(
            DatabaseError("x", details={"error_code": "42703"})
        )
        assert QueryOrchestrator._is_schema_mismatch_error(
            DatabaseError('relation "t" does not exist')
        )
        assert not QueryOrchestrator._is_schema_mismatch_error(
            DatabaseError("connection refused", details={"error_code": "08001"})
        )

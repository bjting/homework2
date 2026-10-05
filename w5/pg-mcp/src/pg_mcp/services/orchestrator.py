"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation. It integrates the resilience layer (rate limiting, circuit breaker,
retry with exponential backoff) and the observability layer (metrics, request
tracing) into the actual request processing flow.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, ClassVar

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    LLMTimeoutError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. Each request:

    1. Runs inside a request-tracing context (request_id propagation)
    2. Passes through the query-level rate limiter (concurrency control)
    3. Routes to the executor of the *resolved* database (multi-DB support)
    4. Retries transient LLM failures with exponential backoff
    5. Records Prometheus metrics for every outcome

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     sql_executors={"db1": executor1, "db2": executor2},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"db1": pool1, "db2": pool2},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="db2"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executors: dict[str, SQLExecutor],
        result_validator: ResultValidator,
        schema_cache: SchemaCache,
        pools: dict[str, Pool],
        resilience_config: ResilienceConfig,
        validation_config: ValidationConfig,
        metrics: MetricsCollector | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        security_policy_prompt: str | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executors: Mapping of database names to their executors.
                Each request is routed to the executor of the resolved
                database, so queries can never run against the wrong one.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries, backoff,
                circuit breaker, and rate limits.
            validation_config: Validation configuration including thresholds.
            metrics: Optional metrics collector. When provided, request
                outcomes, LLM calls, and durations are recorded.
            rate_limiter: Optional shared rate limiter. When omitted, one is
                created from ``resilience_config`` limits.
            security_policy_prompt: Optional blocked-objects description
                injected into the SQL generation prompt, so the model answers
                ACCESS_DENIED instead of substituting blocked objects.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.sql_executors = sql_executors
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools
        self.resilience_config = resilience_config
        self.validation_config = validation_config
        self.metrics = metrics
        self.security_policy_prompt = security_policy_prompt

        # Rate limiting for the request path (query + LLM concurrency)
        self.rate_limiter = rate_limiter or MultiRateLimiter(
            query_limit=resilience_config.query_rate_limit,
            llm_limit=resilience_config.llm_rate_limit,
        )

        # Circuit breaker for LLM calls
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=resilience_config.circuit_breaker_threshold,
            recovery_timeout=resilience_config.circuit_breaker_timeout,
        )

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Generate request_id and open a tracing context
        2. Acquire a query rate-limiter slot
        3. Resolve database and route to its dedicated executor
        4. Load schema from cache
        5. Generate and validate SQL with retry + exponential backoff
        6. Execute SQL (if return_type == RESULT)
        7. Validate results (optional)
        8. Record metrics and return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        request_id = str(uuid.uuid4())
        async with request_context(request_id):
            return await self._execute_query_traced(request, request_id)

    async def _execute_query_traced(
        self,
        request: QueryRequest,
        request_id: str,
    ) -> QueryResponse:
        """Run the pipeline with rate limiting, routing, and metrics.

        Args:
            request: Query request containing question and parameters.
            request_id: Request ID for tracking.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        database_name = "unresolved"
        status = ErrorCode.INTERNAL_ERROR.value
        started = time.monotonic()

        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        try:
            # Step 0: Acquire a query rate-limiter slot (concurrency control)
            acquired = await self.rate_limiter.query_limiter.acquire(
                timeout=self.resilience_config.rate_limit_timeout,
            )
            if not acquired:
                stats = self.rate_limiter.query_limiter.get_stats()
                raise RateLimitExceededError(
                    message=(
                        "Query rate limit exceeded: too many concurrent requests. "
                        f"Try again later (active={stats['active_count']}, "
                        f"max={stats['max_concurrent']})."
                    ),
                    details=stats,
                )
            try:
                # Step 1: Resolve database and its dedicated executor
                database_name = self._resolve_database(request.database)
                executor = self.sql_executors.get(database_name)
                if executor is None:
                    raise DatabaseError(
                        message=f"No SQL executor configured for database '{database_name}'",
                        details={
                            "database": database_name,
                            "available_databases": list(self.sql_executors.keys()),
                        },
                    )
                logger.debug(
                    "Resolved database",
                    extra={"request_id": request_id, "database": database_name},
                )

                # Step 2: Get schema from cache
                schema = await self._get_schema(database_name, request_id)

                # Step 3: Generate and validate SQL with retry logic
                generated_sql, validation_result, tokens_used = (
                    await self._generate_sql_with_retry(
                        question=request.question,
                        schema=schema,
                        request_id=request_id,
                    )
                )

                # Step 4: If return_type is SQL, return early
                if request.return_type == ReturnType.SQL:
                    status = ErrorCode.SUCCESS.value
                    logger.info(
                        "Returning SQL only",
                        extra={"request_id": request_id, "sql_length": len(generated_sql)},
                    )
                    return QueryResponse(
                        success=True,
                        request_id=request_id,
                        generated_sql=generated_sql,
                        validation=validation_result,
                        data=None,
                        error=None,
                        confidence=100,
                        tokens_used=tokens_used,
                    )

                # Step 5: Execute SQL on the resolved database's executor.
                # Schema-mismatch DB errors (e.g. a hallucinated table name)
                # loop back to the LLM with the database error as feedback.
                logger.debug("Executing SQL", extra={"request_id": request_id})
                start_time = time.monotonic()
                results, total_count, final_sql = await self._execute_with_feedback_retry(
                    executor=executor,
                    sql=generated_sql,
                    question=request.question,
                    schema=schema,
                    request_id=request_id,
                )
                generated_sql = final_sql
                execution_time_ms = (time.monotonic() - start_time) * 1000

                if self.metrics is not None:
                    self.metrics.observe_db_query_duration(execution_time_ms / 1000)

                logger.info(
                    "SQL executed successfully",
                    extra={
                        "request_id": request_id,
                        "database": database_name,
                        "row_count": total_count,
                        "execution_time_ms": execution_time_ms,
                    },
                )

                # Step 6: Validate results (non-blocking, failures don't fail the request)
                result_confidence = await self._validate_results_safely(
                    question=request.question,
                    sql=generated_sql,
                    results=results,
                    row_count=total_count,
                    request_id=request_id,
                )

                status = ErrorCode.SUCCESS.value

                # Step 7: Build successful response
                query_result = QueryResult(
                    columns=list(results[0].keys()) if results else [],
                    rows=results,
                    row_count=len(results),  # Limited row count (after max_rows applied)
                    execution_time_ms=execution_time_ms,
                )

                return QueryResponse(
                    success=True,
                    request_id=request_id,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=query_result,
                    error=None,
                    confidence=result_confidence,
                    tokens_used=tokens_used,
                )
            finally:
                self.rate_limiter.query_limiter.release()

        except PgMcpError as e:
            status = e.code.value
            self._record_rejection(e)
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": e.message,
                },
            )
            return QueryResponse(
                success=False,
                request_id=request_id,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code.value,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=None,
            )
        except Exception as e:
            status = ErrorCode.INTERNAL_ERROR.value
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                request_id=request_id,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
            )
        finally:
            if self.metrics is not None:
                self.metrics.increment_query_request(status=status, database=database_name)
                self.metrics.query_duration.observe(time.monotonic() - started)

    # Postgres SQLSTATE codes for schema-mismatch errors an LLM can fix when
    # given the database error as feedback (undefined table/column/function)
    RETRYABLE_SQLSTATES: ClassVar = {"42P01", "42703", "42883"}

    @classmethod
    def _is_schema_mismatch_error(cls, error: DatabaseError) -> bool:
        """Whether the DB error is an undefined table/column/function error.

        These errors usually mean the LLM hallucinated an object name; feeding
        the error back lets it regenerate against the real schema.

        Args:
            error: The DatabaseError raised by the executor.

        Returns:
            bool: True if the error is retryable with feedback.
        """
        sqlstate = error.details.get("error_code")
        if sqlstate in cls.RETRYABLE_SQLSTATES:
            return True
        message = str(error).lower()
        return "does not exist" in message and any(
            token in message for token in ("relation", "column", "function")
        )

    async def _execute_with_feedback_retry(
        self,
        executor: Any,
        sql: str,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[list[dict[str, Any]], int, str]:
        """Execute SQL, regenerating with feedback on schema-mismatch errors.

        Args:
            executor: The resolved database's SQL executor.
            sql: The generated SQL to execute.
            question: User's original question (for regeneration context).
            schema: Database schema (passed to regeneration and validation).
            request_id: Request ID for tracking.

        Returns:
            tuple: (results, total_row_count, final_executed_sql)

        Raises:
            DatabaseError: On non-retryable DB errors or exhausted retries.
            SecurityViolationError: If regenerated SQL fails validation.
        """
        max_retries = self.resilience_config.max_retries
        current_sql = sql

        for attempt in range(max_retries + 1):
            try:
                results, total_count = await executor.execute(current_sql)
                return results, total_count, current_sql
            except DatabaseError as e:
                if attempt >= max_retries or not self._is_schema_mismatch_error(e):
                    raise
                logger.warning(
                    "Execution failed with schema mismatch, regenerating with feedback",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "error": str(e)[:160],
                    },
                )
                current_sql = await self._generate_via_limiter(
                    question=question,
                    schema=schema,
                    previous_attempt=current_sql,
                    error_feedback=str(e),
                    request_id=request_id,
                )
                try:
                    self.sql_validator.validate_or_raise(current_sql, schema=schema)
                except (SecurityViolationError, SQLParseError):
                    self.circuit_breaker.record_failure()
                    raise
                self.circuit_breaker.record_success()

        raise DatabaseError(
            message="SQL execution failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _get_schema(self, database_name: str, request_id: str) -> Any:
        """Load schema for the database from cache, falling back to the pool.

        Args:
            database_name: Resolved database name.
            request_id: Request ID for tracking.

        Returns:
            DatabaseSchema for the database.

        Raises:
            DatabaseError: If no pool exists for the database.
            SchemaLoadError: If schema loading fails.
        """
        schema = self.schema_cache.get(database_name)
        if schema is not None:
            return schema

        pool = self.pools.get(database_name)
        if pool is None:
            raise DatabaseError(
                message=f"No connection pool available for database '{database_name}'",
                details={"database": database_name},
            )
        try:
            return await self.schema_cache.load(database_name, pool)
        except Exception as e:
            raise SchemaLoadError(
                message=f"Failed to load schema for database '{database_name}': {e!s}",
                details={"database": database_name, "error": str(e)},
            ) from e

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        if database is not None:
            # Validate specified database exists
            if database not in self.pools:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": list(self.pools.keys()),
                    },
                )
            return database

        # Auto-select if only one database available
        available_dbs = list(self.pools.keys())
        if len(available_dbs) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available_dbs) == 1:
            return available_dbs[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available_dbs},
        )

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry, backoff, and circuit breaking.

        Retry policy:
        - Validation failures (security/parse): retry immediately with the
          error message as feedback for the next generation attempt.
        - Transient LLM failures (timeouts): retry with exponential backoff
          (``retry_delay * backoff_factor ** attempt``).
        - Every attempt passes through the LLM rate limiter and records
          call/latency metrics.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            RateLimitExceededError: If the LLM rate limiter slot cannot be
                acquired within the configured timeout.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     request_id="123",
            ... )
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL through the LLM rate limiter, with metrics
                generated_sql = await self._generate_via_limiter(
                    question=question,
                    schema=schema,
                    previous_attempt=previous_sql,
                    error_feedback=error_feedback,
                    request_id=request_id,
                )

                # Note: tokens_used would come from OpenAI response metadata if available
                # For now, we don't extract it, but it can be added later

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL (schema enables bare-column resolution for
                # blocked-column checks, e.g. `SELECT email FROM users`)
                try:
                    self.sql_validator.validate_or_raise(generated_sql, schema=schema)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    self._record_rejection(validation_error)
                    if attempt < max_retries:
                        # Retry with feedback
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        continue

                    # Out of retries, record failure and raise
                    self.circuit_breaker.record_failure()
                    logger.error(
                        "SQL validation failed after all retries",
                        extra={
                            "request_id": request_id,
                            "attempts": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                # Build validation result
                validation_result = ValidationResult(
                    is_valid=True,
                    is_select=True,
                    allows_data_modification=False,
                    uses_blocked_functions=[],
                    error_message=None,
                )

                return generated_sql, validation_result, tokens_used

            except LLMTimeoutError as e:
                # Transient LLM failure: retry with exponential backoff
                if attempt < max_retries:
                    delay = self.resilience_config.retry_delay * (
                        self.resilience_config.backoff_factor**attempt
                    )
                    logger.warning(
                        "Transient LLM failure, retrying with backoff",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                            "error": str(e),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue

                self.circuit_breaker.record_failure()
                logger.error(
                    "Transient LLM failure after all retries",
                    extra={"request_id": request_id, "attempts": attempt + 1},
                )
                raise
            except (SecurityViolationError, SQLParseError):
                # Re-raised after circuit-breaker recording in the inner handler
                raise
            except (LLMError, RateLimitExceededError):
                # Generation/rate-limit failures count toward the circuit breaker
                self.circuit_breaker.record_failure()
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _generate_via_limiter(
        self,
        question: str,
        schema: Any,
        previous_attempt: str | None,
        error_feedback: str | None,
        request_id: str,
    ) -> str:
        """Call the SQL generator under the LLM rate limiter with metrics.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            previous_attempt: Previously generated SQL that failed (for retry).
            error_feedback: Error message from previous attempt (for retry).
            request_id: Request ID for tracking.

        Returns:
            str: Generated SQL query.

        Raises:
            RateLimitExceededError: If no LLM slot becomes available in time.
        """
        try:
            async with self.rate_limiter.for_llm(
                timeout=self.resilience_config.rate_limit_timeout,
            ):
                if self.metrics is not None:
                    self.metrics.increment_llm_call("generate_sql")
                call_started = time.monotonic()
                try:
                    return await self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_attempt,
                        error_feedback=error_feedback,
                        security_policy=self.security_policy_prompt,
                    )
                finally:
                    if self.metrics is not None:
                        self.metrics.observe_llm_latency(
                            "generate_sql", time.monotonic() - call_started
                        )
        except TimeoutError as e:
            logger.warning(
                "LLM rate limit timeout",
                extra={"request_id": request_id},
            )
            raise RateLimitExceededError(
                message=(
                    "LLM rate limit exceeded: too many concurrent generation "
                    "requests. Try again later."
                ),
                details=self.rate_limiter.llm_limiter.get_stats(),
            ) from e

    def _record_rejection(self, error: PgMcpError) -> None:
        """Record security rejections in metrics (no-op otherwise).

        Args:
            error: The error raised during the pipeline.
        """
        if self.metrics is None:
            return
        if isinstance(error, SecurityViolationError):
            self.metrics.increment_sql_rejected("security_violation")
        elif isinstance(error, SQLParseError):
            self.metrics.increment_sql_rejected("sql_parse_error")

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> int:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. Returns a confidence score.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            int: Confidence score (0-100). Returns 100 if validation disabled/fails.

        Example:
            >>> confidence = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            if self.metrics is not None:
                self.metrics.increment_llm_call("validate_result")
            call_started = time.monotonic()

            validation_result = await self.result_validator.validate(
                question=question,
                sql=sql,
                results=results,
                row_count=row_count,
            )

            if self.metrics is not None:
                self.metrics.observe_llm_latency(
                    "validate_result", time.monotonic() - call_started
                )

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            return validation_result.confidence

        except Exception as e:
            # Log but don't fail the query
            logger.warning(
                "Result validation failed, continuing with default confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 100  # Default to high confidence if validation fails

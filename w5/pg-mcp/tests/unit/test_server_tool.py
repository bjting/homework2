"""Unit tests for the MCP server query tool and tracing utilities.

These tests exercise the server tool's parameter validation and error
short-circuits without a live database or MCP transport, plus the tracing
context helpers used by the request pipeline.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.models.query import QueryResponse
from pg_mcp.observability.tracing import (
    TraceContext,
    clear_request_id,
    generate_request_id,
    get_request_id,
    request_context,
    set_request_id,
)


class TestTracingContext:
    """Tests for request tracing context propagation."""

    def test_generate_request_id_is_unique(self) -> None:
        """Generated request IDs are unique UUID strings."""
        id1 = generate_request_id()
        id2 = generate_request_id()
        assert id1 != id2
        assert len(id1) == 36

    def test_set_and_clear_request_id(self) -> None:
        """Request ID can be set, read, and cleared."""
        set_request_id("req-x")
        assert get_request_id() == "req-x"
        clear_request_id()
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_request_context_propagates_across_awaits(self) -> None:
        """The request ID set by request_context is visible in awaited calls."""
        seen = []

        async def child() -> None:
            seen.append(get_request_id())

        async with request_context() as req_id:
            await child()

        assert req_id is not None
        assert seen == [req_id]
        assert get_request_id() is None  # Cleaned up on exit

    @pytest.mark.asyncio
    async def test_request_context_restores_previous_id(self) -> None:
        """Nested/after-context state is restored to the previous value."""
        set_request_id("outer")
        async with request_context("inner"):
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"
        clear_request_id()

    def test_trace_context_model(self) -> None:
        """TraceContext stores request metadata."""
        ctx = TraceContext(request_id="req-1", operation="generate_sql")
        assert ctx.request_id == "req-1"
        assert ctx.parent_id is None


class TestQueryTool:
    """Tests for the MCP query tool's short-circuit paths."""

    @pytest.fixture
    def query_tool(self):
        """Import and return the query tool function."""
        from pg_mcp.server import query

        return query

    @pytest.mark.asyncio
    async def test_uninitialized_server_returns_error(self, query_tool) -> None:
        """When the orchestrator is missing, the tool reports SERVER_NOT_INITIALIZED."""
        with patch("pg_mcp.server._orchestrator", None):
            result = await query_tool(question="How many users?")

        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_NOT_INITIALIZED"

    @pytest.mark.asyncio
    async def test_invalid_return_type_rejected(self, query_tool) -> None:
        """return_type values other than sql/result are rejected."""
        mock_orchestrator = MagicMock()
        with (
            patch("pg_mcp.server._orchestrator", mock_orchestrator),
            patch("pg_mcp.server._settings", MagicMock()),
        ):
            result = await query_tool(question="q", return_type="bogus")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_PARAMETER"
        mock_orchestrator.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_request_parameters_rejected(self, query_tool) -> None:
        """An empty question is rejected by QueryRequest validation."""
        mock_orchestrator = MagicMock()
        with (
            patch("pg_mcp.server._orchestrator", mock_orchestrator),
            patch("pg_mcp.server._settings", MagicMock()),
        ):
            result = await query_tool(question="   ")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_successful_query_delegates_to_orchestrator(self, query_tool) -> None:
        """Valid requests are forwarded to the orchestrator and serialized."""
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_query.return_value = QueryResponse(
            success=True,
            request_id="req-1",
            generated_sql="SELECT 1;",
            confidence=100,
        )

        with patch("pg_mcp.server._orchestrator", mock_orchestrator):
            result = await query_tool(question="ping", return_type="sql")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT 1;"
        assert result["request_id"] == "req-1"
        # tokens_used is guaranteed present by the canonical to_dict
        assert result["tokens_used"] == 0

        request = mock_orchestrator.execute_query.call_args.args[0]
        assert request.question == "ping"
        assert request.return_type.value == "sql"

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_internal_error(self, query_tool) -> None:
        """Orchestrator crashes become INTERNAL_ERROR responses, not raises."""
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_query.side_effect = RuntimeError("boom")

        with patch("pg_mcp.server._orchestrator", mock_orchestrator):
            result = await query_tool(question="ping")

        assert result["success"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"
        assert result["tokens_used"] == 0

"""SQL generation service using OpenAI for natural language to SQL conversion.

This module provides the SQLGenerator class that uses OpenAI's LLM to convert
natural language questions into valid PostgreSQL SQL queries.
"""

import re
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from pg_mcp.config.settings import OpenAIConfig
from pg_mcp.models.errors import (
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
    SecurityViolationError,
)
from pg_mcp.prompts.sql_generation import build_system_prompt, build_user_prompt

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion

    from pg_mcp.models.schema import DatabaseSchema


class SQLGenerator:
    """SQL generator using OpenAI for natural language to SQL conversion.

    This class handles the interaction with OpenAI's API to generate SQL queries
    from natural language questions. It includes robust error handling, SQL extraction
    from various response formats, and support for retry scenarios with error feedback.

    Example:
        >>> config = OpenAIConfig(api_key="sk-...", model="gpt-4")
        >>> generator = SQLGenerator(config)
        >>> sql = await generator.generate(
        ...     question="How many users registered today?",
        ...     schema=db_schema
        ... )
    """

    def __init__(self, config: OpenAIConfig) -> None:
        """Initialize SQL generator with OpenAI configuration.

        Args:
            config: OpenAI configuration including API key and model settings.
        """
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            timeout=config.timeout,
            base_url=config.base_url,
        )

    async def generate(
        self,
        question: str,
        schema: "DatabaseSchema",
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
        security_policy: str | None = None,
    ) -> str:
        """Generate SQL statement from natural language question.

        This method sends the question and database schema to OpenAI's API
        and extracts the generated SQL query from the response. It supports
        retry scenarios by accepting previous failed attempts and error feedback.

        Args:
            question: User's natural language question.
            schema: Database schema information for context.
            context: Optional additional context to guide generation.
            previous_attempt: Previously generated SQL that failed (for retry).
            error_feedback: Error message from previous attempt (for retry).

        Returns:
            str: Generated SQL query (without trailing semicolon).

        Raises:
            LLMError: If generation fails or response is invalid.
            LLMTimeoutError: If the API request times out.
            LLMUnavailableError: If the API is unavailable or authentication fails.

        Example:
            >>> # Initial generation
            >>> sql = await generator.generate(
            ...     question="Count all active users",
            ...     schema=db_schema
            ... )
            >>> # Retry with error feedback
            >>> sql = await generator.generate(
            ...     question="Count all active users",
            ...     schema=db_schema,
            ...     previous_attempt="SELECT COUNT(*) FROM user",
            ...     error_feedback='relation "user" does not exist'
            ... )
        """
        user_prompt = build_user_prompt(
            question=question,
            schema=schema,
            context=context,
            previous_attempt=previous_attempt,
            error_feedback=error_feedback,
        )

        try:
            response: ChatCompletion = await self.client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": build_system_prompt(security_policy)},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        except TimeoutError as e:
            raise LLMTimeoutError(
                message=f"OpenAI API request timed out after {self.config.timeout}s",
                details={"timeout": self.config.timeout},
            ) from e
        except Exception as e:
            # Handle various OpenAI errors
            error_msg = str(e)
            if "authentication" in error_msg.lower() or "api_key" in error_msg.lower():
                raise LLMUnavailableError(
                    message="OpenAI API authentication failed - check API key",
                    details={"error": error_msg},
                ) from e
            if "rate_limit" in error_msg.lower():
                raise LLMUnavailableError(
                    message="OpenAI API rate limit exceeded",
                    details={"error": error_msg},
                ) from e
            raise LLMError(
                message=f"OpenAI API request failed: {error_msg}",
                details={"error": error_msg},
            ) from e

        # Extract SQL from response
        if not response.choices:
            raise LLMError(
                message="OpenAI returned empty response",
                details={"response": response.model_dump()},
            )

        content = response.choices[0].message.content
        if not content:
            raise LLMError(
                message="OpenAI returned empty message content",
                details={"response": response.model_dump()},
            )

        # The model signals that the question requires blocked objects; reject
        # with a user-friendly message instead of searching for SQL
        denial = self._extract_access_denial(content)
        if denial:
            raise SecurityViolationError(*denial)

        sql = self._extract_sql(content)
        if not sql:
            raise LLMError(
                message="Failed to extract SQL from OpenAI response",
                details={"content": content},
            )

        return sql

    def _extract_access_denial(self, content: str) -> tuple[str, dict[str, Any]] | None:
        """Detect the ACCESS_DENIED protocol signal in the model output.

        The security policy instructs the model to reply with
        ``ACCESS_DENIED: <object>`` when the question requires blocked
        tables/columns, instead of substituting other objects.

        Args:
            content: Raw content from LLM response.

        Returns:
            tuple | None: (message, details) for SecurityViolationError, or None.
        """
        for line in content.splitlines():
            line = line.strip().strip("`").strip()
            if line.upper().startswith("ACCESS_DENIED"):
                obj = line.split(":", 1)[1].strip().strip("\"'") if ":" in line else "受限对象"
                if "." in obj:
                    message = (
                        f'您没有权限查看列 "{obj}"：该列已被管理员列为敏感数据，'
                        "不会出现在查询结果中。请调整问题，改查其他列。"
                    )
                else:
                    message = (
                        f'您没有权限查看表 "{obj}"：该表已被管理员列为受保护对象，'
                        "无法查询其内容。如需访问，请联系管理员申请权限。"
                    )
                return message, {"access_denied": True, "object": obj}
        return None

    def _extract_sql(self, content: str) -> str | None:
        """Extract SQL query from LLM response content.

        This method implements a multi-strategy approach to extract SQL from
        various response formats that LLMs might generate:

        1. Try to match ```sql ... ``` code blocks (preferred format)
        2. Try to match generic ``` ... ``` code blocks
        3. Try to find SELECT/WITH statements in plain text
        4. Check if entire content looks like SQL

        Args:
            content: Raw content from LLM response.

        Returns:
            str | None: Extracted SQL query, or None if extraction fails.

        Example:
            >>> generator._extract_sql("```sql\\nSELECT 1;\\n```")
            'SELECT 1;'
            >>> generator._extract_sql("SELECT * FROM users;")
            'SELECT * FROM users;'
        """
        if not content:
            return None

        content = content.strip()

        # Strategy 1: Match ```sql ... ``` or ``` ... ``` code blocks
        code_block_pattern = r"```(?:sql)?\s*\n?(.*?)\n?```"
        matches = re.findall(code_block_pattern, content, re.DOTALL | re.IGNORECASE)

        if matches:
            sql = matches[0].strip()
            # Remove trailing semicolon for consistency
            return sql.rstrip(";") + ";"

        # Strategy 2: Find SELECT/WITH statements in plain text
        sql_pattern = r"((?:WITH|SELECT)\s+.*?)(?:;|$)"
        matches = re.findall(sql_pattern, content, re.DOTALL | re.IGNORECASE)

        if matches:
            sql = matches[0].strip()
            return sql.rstrip(";") + ";"

        # Strategy 3: Check if entire content looks like SQL
        if content.upper().startswith(("SELECT", "WITH")):
            return content.rstrip(";") + ";"

        return None

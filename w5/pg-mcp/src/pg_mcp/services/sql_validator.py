"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

from typing import TYPE_CHECKING, ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError

if TYPE_CHECKING:
    from pg_mcp.models.schema import DatabaseSchema


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {exp.Select, exp.Union, exp.Intersect, exp.Except}

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select,
        exp.Union,
        exp.Intersect,
        exp.Except,
        exp.With,
        exp.Subquery,
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    # DML statement types (conditionally allowed via allow_write_operations)
    DML_STATEMENT_TYPES: ClassVar = {exp.Insert, exp.Update, exp.Delete, exp.Merge}

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool = False,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
            blocked_columns: Optional list of column names to block access to.
            allow_explain: Whether to allow EXPLAIN statements.
        """
        self.config = config
        self.blocked_tables = {t.lower() for t in (blocked_tables or [])}
        self.blocked_columns = {c.lower() for c in (blocked_columns or [])}
        self.allow_explain = allow_explain

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(
        self,
        sql: str,
        schema: "DatabaseSchema | None" = None,
    ) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.
            schema: Optional live database schema (enables bare-column
                resolution for blocked-column checks).

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql, schema=schema)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str, schema: "DatabaseSchema | None" = None) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.
            schema: Optional live database schema. When provided, bare column
                references (no table prefix) can be resolved to their owning
                tables, closing the bare-column bypass of qualified blocks
                (e.g. ``SELECT email FROM users`` vs blocked ``users.email``).

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None or isinstance(statement, type(None)):
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot 28.5.0)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError("EXPLAIN statements are not allowed")
                # EXPLAIN is read-only and safe - it only shows query plans without executing.
                # sqlglot 28.5.0 cannot parse EXPLAIN syntax reliably (falls back to Command),
                # so we don't attempt to validate the inner query string to avoid false positives.
                # Even "EXPLAIN DELETE" is safe as it won't actually delete data.
                return None
            else:
                # Other commands are not allowed
                raise SecurityViolationError(
                    f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
                )

        # Handle CTE (WITH) statements - extract the main query
        if isinstance(statement, exp.With):
            # WITH statements are allowed, but we need to validate the main query
            if statement.this:
                main_query = statement.this
            else:
                raise SQLParseError("WITH statement has no main query")
        else:
            main_query = statement

        # Perform security checks
        if error := self._check_statement_type(main_query):
            raise SecurityViolationError(error)

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_star_access(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement, schema):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # When write operations are explicitly allowed via config, DML
        # (INSERT/UPDATE/DELETE/MERGE) passes validation; DDL and admin
        # statements always remain forbidden. The executor correspondingly
        # opens a writable transaction only when this flag is set.
        forbidden_types = self.FORBIDDEN_STATEMENT_TYPES
        if self.config.allow_write_operations:
            forbidden_types = forbidden_types - self.DML_STATEMENT_TYPES

        # Check for forbidden statement types
        for forbidden_type in forbidden_types:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT, set operations, or
        # permitted DML when writes are enabled)
        if isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            return None

        if isinstance(statement, tuple(self.DML_STATEMENT_TYPES)):
            if self.config.allow_write_operations:
                return None
            stmt_name = type(statement).__name__.upper()
            return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        stmt_type = type(statement).__name__
        return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""

            if table_name in self.blocked_tables:
                return (
                    f'您没有权限查看表 "{table_name}"：该表已被管理员列为受保护对象，'
                    "无法查询其内容。如需访问，请联系管理员申请权限。"
                )

        return None

    def _check_star_access(self, statement: exp.Expression) -> str | None:
        """Block ``SELECT *`` when column restrictions are configured.

        ``SELECT *`` produces no column nodes, so without this check it would
        silently bypass blocked_columns and leak sensitive columns.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        for _star in statement.find_all(exp.Star):
            return (
                "当前环境已启用敏感列保护，不允许使用 SELECT *；"
                "请在查询中明确列出所需的列（不含受限列）。"
            )
        return None

    def _build_alias_map(self, statement: exp.Expression) -> dict[str, str]:
        """Build a mapping of table alias -> real table name.

        Found in FROM/JOIN clauses, e.g. ``FROM users u`` yields ``{"u": "users"}``.
        Needed because column nodes reference the alias, so a blocked qualified
        name like ``users.email`` would otherwise be bypassed via ``u.email``.

        Args:
            statement: Parsed SQL statement.

        Returns:
            dict[str, str]: Alias (lowercase) to real table name (lowercase).
        """
        aliases: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            if table.alias and table.name:
                aliases[table.alias.lower()] = table.name.lower()
        return aliases

    def _check_blocked_columns(
        self,
        statement: exp.Expression,
        schema: "DatabaseSchema | None" = None,
    ) -> str | None:
        """Check for access to blocked columns.

        Column references are resolved through table aliases before matching,
        so ``SELECT u.email FROM users u`` is caught by a ``users.email`` block.
        When a schema is provided, bare columns (no table prefix) are resolved
        to their owning tables, so ``SELECT email FROM users`` is caught too.

        Args:
            statement: Parsed SQL statement.
            schema: Optional live database schema for bare-column resolution.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        alias_map = self._build_alias_map(statement)

        def blocked_column_message(qualified: str) -> str:
            """User-facing message for a blocked column reference."""
            return (
                f'您没有权限查看列 "{qualified}"：该列已被管理员列为敏感数据，'
                "不会出现在查询结果中。请调整问题，改查其他列。"
            )

        # table name (lowercase) -> set of column names (lowercase)
        schema_columns: dict[str, set[str]] = {}
        if schema is not None:
            for table in schema.tables:
                schema_columns[table.table_name.lower()] = {
                    col.name.lower() for col in table.columns
                }

        # Find all column references
        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""
            if not column_name:
                continue

            # Check for exact match (bare blocked column, e.g. "password")
            if column_name in self.blocked_columns:
                return blocked_column_message(column_name)

            # Check qualified names, resolving aliases to real table names
            if column.table:
                table_ref = column.table.lower()
                real_table = alias_map.get(table_ref, table_ref)
                if real_table != table_ref and f"{table_ref}.{column_name}" in self.blocked_columns:
                    return blocked_column_message(f"{table_ref}.{column_name}")
                if f"{real_table}.{column_name}" in self.blocked_columns:
                    return blocked_column_message(f"{real_table}.{column_name}")
            elif schema_columns:
                # Bare column with no table reference: it can only belong to
                # tables named in this statement's FROM/JOIN clauses
                from_tables = {t.name.lower() for t in statement.find_all(exp.Table) if t.name}
                for table_name in from_tables:
                    columns = schema_columns.get(table_name, set())
                    if (
                        column_name in columns
                        and f"{table_name}.{column_name}" in self.blocked_columns
                    ):
                        return blocked_column_message(f"{table_name}.{column_name}")

        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all subqueries
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this:
                inner_stmt = subquery.this

                # Check if the inner statement is a forbidden type
                for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                    if isinstance(inner_stmt, forbidden_type):
                        stmt_name = forbidden_type.__name__.upper()
                        return f"{stmt_name} statements in subqueries are not allowed"

                # Ensure it's a SELECT
                if not isinstance(inner_stmt, (exp.Select, exp.With)):
                    return "Subqueries must contain only SELECT statements"

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e

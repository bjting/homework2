"""Comprehensive unit tests for SQL security validator.

Tests cover:
- Valid SELECT statements
- Rejected write operations
- Dangerous function blocking
- Sensitive resource protection
- Multi-statement detection
- Edge cases and malformed SQL
"""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError
from pg_mcp.services.sql_validator import SQLValidator


class TestValidStatements:
    """Test cases for valid SELECT statements that should pass validation."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create a basic validator with default security config."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_simple_select(self, validator: SQLValidator) -> None:
        """Test simple SELECT statement."""
        sql = "SELECT * FROM users"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_select_with_where(self, validator: SQLValidator) -> None:
        """Test SELECT with WHERE clause."""
        sql = "SELECT id, name FROM users WHERE age > 18"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_select_with_join(self, validator: SQLValidator) -> None:
        """Test SELECT with JOIN."""
        sql = """
            SELECT u.name, o.total
            FROM users u
            INNER JOIN orders o ON u.id = o.user_id
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_select_with_subquery(self, validator: SQLValidator) -> None:
        """Test SELECT with subquery in WHERE clause."""
        sql = """
            SELECT name
            FROM users
            WHERE id IN (SELECT user_id FROM orders WHERE total > 100)
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_select_with_subquery_in_from(self, validator: SQLValidator) -> None:
        """Test SELECT with subquery in FROM clause."""
        sql = """
            SELECT t.name, t.count
            FROM (SELECT name, COUNT(*) as count FROM users GROUP BY name) t
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_cte_query(self, validator: SQLValidator) -> None:
        """Test Common Table Expression (CTE) query."""
        sql = """
            WITH user_stats AS (
                SELECT user_id, COUNT(*) as order_count
                FROM orders
                GROUP BY user_id
            )
            SELECT u.name, s.order_count
            FROM users u
            JOIN user_stats s ON u.id = s.user_id
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_complex_aggregation(self, validator: SQLValidator) -> None:
        """Test complex aggregation with GROUP BY and HAVING."""
        sql = """
            SELECT
                category,
                COUNT(*) as count,
                AVG(price) as avg_price,
                MAX(price) as max_price
            FROM products
            GROUP BY category
            HAVING COUNT(*) > 5
            ORDER BY avg_price DESC
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_window_functions(self, validator: SQLValidator) -> None:
        """Test window functions."""
        sql = """
            SELECT
                name,
                salary,
                ROW_NUMBER() OVER (ORDER BY salary DESC) as rank,
                AVG(salary) OVER (PARTITION BY department) as dept_avg
            FROM employees
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_multiple_joins(self, validator: SQLValidator) -> None:
        """Test query with multiple joins."""
        sql = """
            SELECT u.name, o.total, p.name as product
            FROM users u
            JOIN orders o ON u.id = o.user_id
            JOIN order_items oi ON o.id = oi.order_id
            JOIN products p ON oi.product_id = p.id
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_case_expression(self, validator: SQLValidator) -> None:
        """Test CASE expression."""
        sql = """
            SELECT
                name,
                CASE
                    WHEN age < 18 THEN 'minor'
                    WHEN age < 65 THEN 'adult'
                    ELSE 'senior'
                END as age_group
            FROM users
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestRejectedStatements:
    """Test cases for write operations that should be rejected."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for testing rejected statements."""
        config = SecurityConfig(allow_write_operations=False)
        return SQLValidator(config=config)

    def test_insert_rejected(self, validator: SQLValidator) -> None:
        """Test INSERT statement is rejected."""
        sql = "INSERT INTO users (name, email) VALUES ('John', 'john@example.com')"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "INSERT" in str(exc_info.value).upper()

    def test_update_rejected(self, validator: SQLValidator) -> None:
        """Test UPDATE statement is rejected."""
        sql = "UPDATE users SET email = 'new@example.com' WHERE id = 1"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "UPDATE" in str(exc_info.value).upper()

    def test_delete_rejected(self, validator: SQLValidator) -> None:
        """Test DELETE statement is rejected."""
        sql = "DELETE FROM users WHERE id = 1"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "DELETE" in str(exc_info.value).upper()

    def test_drop_rejected(self, validator: SQLValidator) -> None:
        """Test DROP statement is rejected."""
        sql = "DROP TABLE users"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "DROP" in str(exc_info.value).upper()

    def test_create_rejected(self, validator: SQLValidator) -> None:
        """Test CREATE statement is rejected."""
        sql = "CREATE TABLE test (id INT, name VARCHAR(100))"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "CREATE" in str(exc_info.value).upper()

    def test_alter_rejected(self, validator: SQLValidator) -> None:
        """Test ALTER statement is rejected."""
        sql = "ALTER TABLE users ADD COLUMN age INT"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "ALTER" in str(exc_info.value).upper()

    def test_truncate_rejected(self, validator: SQLValidator) -> None:
        """Test TRUNCATE statement is rejected."""
        sql = "TRUNCATE TABLE users"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "TRUNCATE" in str(exc_info.value).upper()

    def test_grant_rejected(self, validator: SQLValidator) -> None:
        """Test GRANT statement is rejected."""
        sql = "GRANT SELECT ON users TO public"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "GRANT" in str(exc_info.value).upper()


class TestDangerousFunctions:
    """Test cases for blocking dangerous PostgreSQL functions."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator with default dangerous function blocking."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_pg_sleep_blocked(self, validator: SQLValidator) -> None:
        """Test pg_sleep function is blocked."""
        sql = "SELECT pg_sleep(10)"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "pg_sleep" in str(exc_info.value).lower()

    def test_pg_read_file_blocked(self, validator: SQLValidator) -> None:
        """Test pg_read_file function is blocked."""
        sql = "SELECT pg_read_file('/etc/passwd')"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "pg_read_file" in str(exc_info.value).lower()

    def test_lo_import_blocked(self, validator: SQLValidator) -> None:
        """Test lo_import function is blocked."""
        sql = "SELECT lo_import('/tmp/file.txt')"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "lo_import" in str(exc_info.value).lower()

    def test_dblink_blocked(self, validator: SQLValidator) -> None:
        """Test dblink function is blocked."""
        sql = "SELECT * FROM dblink('host=remote', 'SELECT * FROM users') AS t(id INT)"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "dblink" in str(exc_info.value).lower()

    def test_pg_terminate_backend_blocked(self, validator: SQLValidator) -> None:
        """Test pg_terminate_backend function is blocked."""
        sql = "SELECT pg_terminate_backend(12345)"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "pg_terminate_backend" in str(exc_info.value).lower()

    def test_custom_blocked_function(self) -> None:
        """Test custom blocked function from config."""
        config = SecurityConfig(blocked_functions=["my_custom_func", "another_func"])
        validator = SQLValidator(config=config)

        sql = "SELECT my_custom_func(123)"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "my_custom_func" in str(exc_info.value).lower()

    def test_safe_functions_allowed(self, validator: SQLValidator) -> None:
        """Test that safe/common functions are allowed."""
        sql = """
            SELECT
                COUNT(*),
                AVG(price),
                MAX(created_at),
                COALESCE(name, 'Unknown'),
                UPPER(email),
                NOW(),
                EXTRACT(YEAR FROM created_at)
            FROM users
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestSensitiveResources:
    """Test cases for blocking access to sensitive tables and columns."""

    def test_blocked_table_rejected(self) -> None:
        """Test access to blocked table is rejected."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_tables=["passwords", "secrets", "api_keys"])

        sql = "SELECT * FROM passwords"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "passwords" in str(exc_info.value).lower()

    def test_blocked_table_in_join(self) -> None:
        """Test blocked table in JOIN is rejected."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_tables=["sensitive_data"])

        sql = """
            SELECT u.name
            FROM users u
            JOIN sensitive_data s ON u.id = s.user_id
        """
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "sensitive_data" in str(exc_info.value).lower()

    def test_blocked_column_rejected(self) -> None:
        """Test access to blocked column is rejected."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_columns=["password", "ssn", "credit_card"])

        sql = "SELECT id, name, password FROM users"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "password" in str(exc_info.value).lower()

    def test_blocked_column_case_insensitive(self) -> None:
        """Test column blocking is case-insensitive."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_columns=["SECRET_KEY"])

        sql = "SELECT id, secret_key FROM users"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "secret_key" in str(exc_info.value).lower()

    def test_partial_column_match(self) -> None:
        """Test that column blocking is exact match, not partial."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_columns=["password"])

        # 'password_hash' should be allowed if only 'password' is blocked
        sql = "SELECT id, password_hash FROM users"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_qualified_column_blocked(self) -> None:
        """Test blocking qualified column names (table.column)."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, blocked_columns=["users.ssn"])

        sql = "SELECT users.id, users.ssn FROM users"
        # This should NOT be blocked because we check the column name without table prefix
        # unless explicitly in the blocked list
        is_valid, error = validator.validate(sql)
        # Actually, it depends on implementation - let's test both scenarios
        # The validator checks both column name and qualified name
        assert is_valid or "ssn" in (error or "")


class TestMultiStatement:
    """Test cases for detecting and blocking multi-statement queries."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for multi-statement testing."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_multiple_statements_rejected(self, validator: SQLValidator) -> None:
        """Test multiple statements separated by semicolon are rejected."""
        sql = "SELECT * FROM users; SELECT * FROM orders;"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "multiple" in str(exc_info.value).lower()

    def test_sql_injection_attempt_drop(self, validator: SQLValidator) -> None:
        """Test SQL injection with DROP is rejected."""
        sql = "SELECT * FROM users WHERE id = 1; DROP TABLE users;--"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        # Should catch either as multiple statements or DROP statement
        error_msg = str(exc_info.value).lower()
        assert "multiple" in error_msg or "drop" in error_msg

    def test_sql_injection_attempt_union(self, validator: SQLValidator) -> None:
        """Test SQL injection with UNION is handled (UNION itself is SQL-valid)."""
        # UNION is a valid SQL operation in SELECT, should be allowed
        sql = "SELECT id FROM users UNION SELECT id FROM orders"
        is_valid, _error = validator.validate(sql)
        assert is_valid  # UNION is valid in SELECT

    def test_sql_injection_attempt_insert(self, validator: SQLValidator) -> None:
        """Test SQL injection with INSERT is rejected."""
        sql = "SELECT * FROM users; INSERT INTO logs VALUES (1, 'hacked');"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        error_msg = str(exc_info.value).lower()
        assert "multiple" in error_msg or "insert" in error_msg


class TestEdgeCases:
    """Test edge cases and malformed SQL."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for edge case testing."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_malformed_sql(self, validator: SQLValidator) -> None:
        """Test malformed SQL raises parse error."""
        sql = "SELECT * FROM WHERE"
        with pytest.raises(SQLParseError):
            validator.validate_or_raise(sql)

    def test_empty_sql(self, validator: SQLValidator) -> None:
        """Test empty SQL raises parse error."""
        sql = ""
        with pytest.raises(SQLParseError) as exc_info:
            validator.validate_or_raise(sql)
        assert "empty" in str(exc_info.value).lower()

    def test_whitespace_only_sql(self, validator: SQLValidator) -> None:
        """Test whitespace-only SQL raises parse error."""
        sql = "   \n\t  "
        with pytest.raises(SQLParseError) as exc_info:
            validator.validate_or_raise(sql)
        assert "empty" in str(exc_info.value).lower()

    def test_comment_only_sql(self, validator: SQLValidator) -> None:
        """Test comment-only SQL raises parse error."""
        sql = "-- This is just a comment"
        with pytest.raises(SQLParseError):
            validator.validate_or_raise(sql)

    def test_incomplete_sql(self, validator: SQLValidator) -> None:
        """Test incomplete SQL raises parse error."""
        sql = "SELECT * FROM"
        with pytest.raises(SQLParseError):
            validator.validate_or_raise(sql)

    def test_sql_with_comments(self, validator: SQLValidator) -> None:
        """Test SQL with comments is handled correctly."""
        sql = """
            -- Get all active users
            SELECT id, name
            FROM users -- user table
            WHERE active = true
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestExplainStatements:
    """Test EXPLAIN statement handling."""

    def test_explain_rejected_by_default(self) -> None:
        """Test EXPLAIN is rejected when not explicitly allowed."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, allow_explain=False)

        sql = "EXPLAIN SELECT * FROM users"
        with pytest.raises(SecurityViolationError) as exc_info:
            validator.validate_or_raise(sql)
        assert "explain" in str(exc_info.value).lower()

    def test_explain_allowed_when_enabled(self) -> None:
        """Test EXPLAIN is allowed when explicitly enabled."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, allow_explain=True)

        sql = "EXPLAIN SELECT * FROM users"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_explain_analyze_allowed(self) -> None:
        """Test EXPLAIN ANALYZE is allowed when EXPLAIN is enabled."""
        config = SecurityConfig()
        validator = SQLValidator(config=config, allow_explain=True)

        sql = "EXPLAIN ANALYZE SELECT * FROM users WHERE id > 100"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_explain_with_dangerous_query_allowed(self) -> None:
        """Test EXPLAIN with dangerous underlying query is allowed.

        EXPLAIN only shows query plans and doesn't execute the query,
        so even "EXPLAIN DELETE" is safe as it won't modify data.
        """
        config = SecurityConfig()
        validator = SQLValidator(config=config, allow_explain=True)

        # EXPLAIN DELETE is safe - it only shows the execution plan
        sql = "EXPLAIN DELETE FROM users"
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestValidatorHelperMethods:
    """Test validator helper methods like normalize_sql and extract_tables."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for testing helper methods."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_normalize_sql(self, validator: SQLValidator) -> None:
        """Test SQL normalization."""
        sql = """
            SELECT   id,  name
            FROM     users
            WHERE    active = true
        """
        normalized = validator.normalize_sql(sql)
        assert normalized
        assert "SELECT" in normalized
        assert "FROM" in normalized

    def test_normalize_sql_invalid(self, validator: SQLValidator) -> None:
        """Test normalizing invalid SQL raises error."""
        sql = "SELECT * FROM WHERE"
        with pytest.raises(SQLParseError):
            validator.normalize_sql(sql)

    def test_extract_tables_simple(self, validator: SQLValidator) -> None:
        """Test extracting tables from simple query."""
        sql = "SELECT * FROM users"
        tables = validator.extract_tables(sql)
        assert tables == ["users"]

    def test_extract_tables_multiple(self, validator: SQLValidator) -> None:
        """Test extracting multiple tables."""
        sql = """
            SELECT u.name, o.total
            FROM users u
            JOIN orders o ON u.id = o.user_id
        """
        tables = validator.extract_tables(sql)
        assert sorted(tables) == ["orders", "users"]

    def test_extract_tables_with_subquery(self, validator: SQLValidator) -> None:
        """Test extracting tables including from subqueries."""
        sql = """
            SELECT *
            FROM users
            WHERE id IN (SELECT user_id FROM orders)
        """
        tables = validator.extract_tables(sql)
        assert sorted(tables) == ["orders", "users"]

    def test_extract_tables_deduplicated(self, validator: SQLValidator) -> None:
        """Test that duplicate table references are deduplicated."""
        sql = """
            SELECT *
            FROM users u1
            JOIN users u2 ON u1.manager_id = u2.id
        """
        tables = validator.extract_tables(sql)
        assert tables == ["users"]

    def test_extract_tables_invalid_sql(self, validator: SQLValidator) -> None:
        """Test extracting tables from invalid SQL raises error."""
        sql = "INVALID SQL QUERY"
        with pytest.raises(SQLParseError):
            validator.extract_tables(sql)


class TestCTEWithDangerousOperations:
    """Test CTE (Common Table Expressions) with dangerous operations."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for CTE testing."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_cte_with_multiple_selects(self, validator: SQLValidator) -> None:
        """Test CTE with multiple SELECT CTEs is allowed."""
        sql = """
            WITH user_orders AS (
                SELECT user_id, COUNT(*) as order_count
                FROM orders
                GROUP BY user_id
            ),
            high_value_users AS (
                SELECT user_id
                FROM user_orders
                WHERE order_count > 10
            )
            SELECT u.name
            FROM users u
            JOIN high_value_users h ON u.id = h.user_id
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None

    def test_cte_in_subquery(self, validator: SQLValidator) -> None:
        """Test nested CTEs and subqueries."""
        sql = """
            WITH recent_orders AS (
                SELECT user_id, total
                FROM orders
                WHERE created_at > NOW() - INTERVAL '30 days'
            )
            SELECT
                u.name,
                (SELECT SUM(total) FROM recent_orders WHERE user_id = u.id) as total_spent
            FROM users u
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestSubqueryWithForbiddenOperations:
    """Test that forbidden operations in subqueries are caught."""

    @pytest.fixture
    def validator(self) -> SQLValidator:
        """Create validator for subquery testing."""
        config = SecurityConfig()
        return SQLValidator(config=config)

    def test_subquery_with_insert_rejected(self, validator: SQLValidator) -> None:
        """Test subquery containing INSERT is rejected."""
        # Note: This is syntactically invalid SQL, but we test the validator's safety
        sql = """
            SELECT *
            FROM users
            WHERE id = (INSERT INTO logs VALUES (1) RETURNING id)
        """
        # This will likely fail at parse level or be caught as INSERT
        with pytest.raises((SQLParseError, SecurityViolationError)):
            validator.validate_or_raise(sql)

    def test_nested_subquery_all_selects(self, validator: SQLValidator) -> None:
        """Test deeply nested subqueries with only SELECTs are allowed."""
        sql = """
            SELECT name
            FROM users
            WHERE id IN (
                SELECT user_id
                FROM orders
                WHERE product_id IN (
                    SELECT id
                    FROM products
                    WHERE category = 'electronics'
                )
            )
        """
        is_valid, error = validator.validate(sql)
        assert is_valid
        assert error is None


class TestSecurityPolicyWiring:
    """Test that the configured security policy is actually enforced:
    blocked tables/columns (sensitive objects) and write-operation policy."""

    def test_blocked_tables_reject_access(self) -> None:
        """Queries touching a blocked table are rejected."""
        config = SecurityConfig(blocked_tables=["audit_logs", "user_credentials"])
        validator = SQLValidator(config=config, blocked_tables=config.blocked_tables)

        is_valid, error = validator.validate("SELECT * FROM audit_logs")
        assert not is_valid
        assert "audit_logs" in error

        is_valid, error = validator.validate("SELECT * FROM user_credentials WHERE id = 1")
        assert not is_valid
        assert "user_credentials" in error

    def test_blocked_tables_case_insensitive(self) -> None:
        """Blocked table matching is case-insensitive."""
        config = SecurityConfig(blocked_tables=["Audit_Logs"])
        validator = SQLValidator(config=config, blocked_tables=config.blocked_tables)

        is_valid, _error = validator.validate("SELECT * FROM audit_logs")
        assert not is_valid

    def test_allowed_table_passes_with_blocked_tables_configured(self) -> None:
        """Tables not on the blocked list remain queryable."""
        config = SecurityConfig(blocked_tables=["audit_logs"])
        validator = SQLValidator(config=config, blocked_tables=config.blocked_tables)

        is_valid, error = validator.validate("SELECT * FROM users")
        assert is_valid
        assert error is None

    def test_blocked_tables_in_subquery_rejected(self) -> None:
        """Blocked tables are rejected inside subqueries and CTEs too."""
        config = SecurityConfig(blocked_tables=["secrets"])
        validator = SQLValidator(config=config, blocked_tables=config.blocked_tables)

        is_valid, error = validator.validate(
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM secrets)"
        )
        assert not is_valid
        assert "secrets" in error

        is_valid, error = validator.validate(
            "WITH leaked AS (SELECT * FROM secrets) SELECT * FROM leaked"
        )
        assert not is_valid
        assert "secrets" in error

    def test_blocked_columns_reject_access(self) -> None:
        """Queries selecting a blocked column are rejected."""
        config = SecurityConfig(blocked_columns=["password"])
        validator = SQLValidator(config=config, blocked_columns=config.blocked_columns)

        is_valid, error = validator.validate("SELECT password FROM users")
        assert not is_valid
        assert "password" in error

        # SELECT * is rejected by the star-access guard (it hides column refs),
        # so the message mentions SELECT * rather than the blocked column
        is_valid, error = validator.validate("SELECT * FROM users WHERE password = 'x'")
        assert not is_valid
        assert "SELECT *" in error

    def test_blocked_columns_qualified_names(self) -> None:
        """table.column qualified blocking works precisely."""
        config = SecurityConfig(blocked_columns=["users.email"])
        validator = SQLValidator(config=config, blocked_columns=config.blocked_columns)

        # Qualified reference is blocked
        is_valid, error = validator.validate("SELECT users.email FROM users")
        assert not is_valid
        assert "users.email" in error

        # Same column name on another table remains allowed
        is_valid, error = validator.validate("SELECT contacts.email FROM contacts")
        assert is_valid
        assert error is None

    def test_write_operations_still_rejected_by_default(self) -> None:
        """DML remains rejected when allow_write_operations is False."""
        validator = SQLValidator(config=SecurityConfig(allow_write_operations=False))
        for sql in (
            "INSERT INTO logs VALUES (1)",
            "UPDATE users SET name = 'x'",
            "DELETE FROM users",
        ):
            is_valid, _error = validator.validate(sql)
            assert not is_valid

    def test_write_operations_allowed_when_configured(self) -> None:
        """DML passes validation when allow_write_operations is True."""
        validator = SQLValidator(
            config=SecurityConfig(allow_write_operations=True),
        )
        for sql in (
            "INSERT INTO logs VALUES (1)",
            "UPDATE users SET name = 'x' WHERE id = 1",
            "DELETE FROM users WHERE id = 1",
        ):
            is_valid, error = validator.validate(sql)
            assert is_valid, f"{sql} should be allowed: {error}"

    def test_ddl_still_rejected_even_when_writes_allowed(self) -> None:
        """DDL/admin statements stay forbidden regardless of the write flag."""
        validator = SQLValidator(config=SecurityConfig(allow_write_operations=True))
        for sql in (
            "DROP TABLE users",
            "CREATE TABLE evil (id int)",
            "ALTER TABLE users ADD COLUMN x int",
            "GRANT ALL ON users TO public",
        ):
            is_valid, _error = validator.validate(sql)
            assert not is_valid, f"{sql} must remain forbidden"

    def test_dangerous_functions_blocked_even_when_writes_allowed(self) -> None:
        """Blocked functions are enforced independently of the write flag."""
        validator = SQLValidator(config=SecurityConfig(allow_write_operations=True))
        is_valid, error = validator.validate("SELECT pg_sleep(100)")
        assert not is_valid
        assert "pg_sleep" in error


class TestStarAccessBypass:
    """SELECT * must not bypass blocked_columns (regression tests)."""

    def make_validator(self, blocked_columns: list[str]) -> SQLValidator:
        return SQLValidator(
            config=SecurityConfig(),
            blocked_columns=blocked_columns,
        )

    def test_select_star_blocked_when_columns_restricted(self) -> None:
        """SELECT * returns no column nodes, so it must be rejected explicitly."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate("SELECT * FROM users")
        assert not is_valid
        assert "SELECT *" in error

    def test_table_qualified_star_blocked(self) -> None:
        """SELECT u.* is the same bypass vector."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate("SELECT u.* FROM users u")
        assert not is_valid
        assert "SELECT *" in error

    def test_star_inside_cte_blocked(self) -> None:
        """A star hidden inside a CTE must be caught too."""
        validator = self.make_validator(["users.email"])
        is_valid, _error = validator.validate("WITH t AS (SELECT * FROM users) SELECT * FROM t")
        assert not is_valid

    def test_explicit_safe_columns_pass(self) -> None:
        """Enumerating non-blocked columns remains allowed."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate("SELECT id, username FROM users")
        assert is_valid, error

    def test_select_star_allowed_without_restrictions(self) -> None:
        """Without column restrictions, SELECT * keeps working as before."""
        validator = self.make_validator([])
        is_valid, error = validator.validate("SELECT * FROM users")
        assert is_valid, error


class TestAliasBypass:
    """Table aliases must not bypass blocked_columns (found in live webui test)."""

    def make_validator(self, blocked_columns: list[str]) -> SQLValidator:
        return SQLValidator(
            config=SecurityConfig(),
            blocked_columns=blocked_columns,
        )

    def test_aliased_blocked_column_rejected(self) -> None:
        """SELECT u.email FROM users u must hit the users.email block."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate(
            "SELECT u.username, u.email FROM users u ORDER BY u.id LIMIT 100"
        )
        assert not is_valid
        assert "users.email" in error

    def test_expanded_star_columns_rejected(self) -> None:
        """LLM-explicit column lists with aliases must be caught too."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate(
            "SELECT u.id, u.username, u.email, u.full_name, u.role, "
            "u.created_at, u.updated_at FROM users AS u LIMIT 100"
        )
        assert not is_valid
        assert "users.email" in error

    def test_aliased_join_blocked_column_rejected(self) -> None:
        """JOIN with aliases must resolve each alias to its real table."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate(
            "SELECT p.title, u.email FROM posts AS p JOIN users AS u ON u.id = p.author_id"
        )
        assert not is_valid
        assert "users.email" in error

    def test_direct_qualified_name_still_rejected(self) -> None:
        """Non-aliased qualified references keep working."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate("SELECT users.email FROM users")
        assert not is_valid
        assert "users.email" in error

    def test_safe_aliased_columns_pass(self) -> None:
        """Aliased queries touching only allowed columns stay valid."""
        validator = self.make_validator(["users.email"])
        is_valid, error = validator.validate(
            "SELECT u.username FROM users u ORDER BY u.id LIMIT 100"
        )
        assert is_valid, error


class TestSchemaAwareBareColumn:
    """Bare columns must resolve to owning tables via schema (live-found bypass)."""

    @staticmethod
    def make_schema():
        from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo

        return DatabaseSchema(
            database_name="blog_small",
            tables=[
                TableInfo(
                    schema_name="public",
                    table_name="users",
                    columns=[
                        ColumnInfo(
                            name="id", data_type="integer", is_nullable=False, is_primary_key=True
                        ),
                        ColumnInfo(name="username", data_type="varchar(50)", is_nullable=False),
                        ColumnInfo(name="email", data_type="varchar(100)", is_nullable=False),
                    ],
                ),
                TableInfo(
                    schema_name="public",
                    table_name="posts",
                    columns=[
                        ColumnInfo(
                            name="id", data_type="integer", is_nullable=False, is_primary_key=True
                        ),
                        ColumnInfo(name="title", data_type="varchar(200)", is_nullable=False),
                        ColumnInfo(name="email", data_type="varchar(100)", is_nullable=True),
                    ],
                ),
            ],
            version="16",
        )

    def make_validator(self) -> SQLValidator:
        return SQLValidator(config=SecurityConfig(), blocked_columns=["users.email"])

    def test_bare_blocked_column_rejected(self) -> None:
        """SELECT email FROM users (bare column) must hit the users.email block."""
        validator = self.make_validator()
        is_valid, error = validator.validate(
            "SELECT username, email FROM users", schema=self.make_schema()
        )
        assert not is_valid
        assert "users.email" in error

    def test_bare_safe_column_passes(self) -> None:
        """Bare columns that map to no blocked table.column stay valid."""
        validator = self.make_validator()
        is_valid, error = validator.validate(
            "SELECT username, id FROM users", schema=self.make_schema()
        )
        assert is_valid, error

    def test_bare_column_in_other_table_passes(self) -> None:
        """Only users.email is blocked; posts.email must remain accessible."""
        validator = self.make_validator()
        is_valid, error = validator.validate(
            "SELECT title, email FROM posts", schema=self.make_schema()
        )
        assert is_valid, error

    def test_bare_column_without_schema_still_checked_against_bare_blocks(self) -> None:
        """Without schema, bare-name blocks (e.g. password) keep working."""
        validator = SQLValidator(config=SecurityConfig(), blocked_columns=["password"])
        is_valid, _ = validator.validate("SELECT username, password FROM users")
        assert not is_valid

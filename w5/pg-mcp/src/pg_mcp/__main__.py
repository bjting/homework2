"""Entry point for PostgreSQL MCP Server (python -m pg_mcp).

Loads the project .env into process environment variables BEFORE any
configuration object is built, because the nested pydantic-settings models
(OpenAIConfig, DatabaseConfig, ...) read os.environ only - the parent
Settings' env_file does not propagate to them.
"""

import anyio
from dotenv import load_dotenv

from pg_mcp.server import mcp


def main() -> None:
    """Start the FastMCP server using stdio transport.

    The server lifecycle is managed through the lifespan context manager,
    which handles configuration loading, pool creation, schema cache
    initialization, service component setup, and graceful shutdown.

    Example:
        Run the server:
        >>> python -m pg_mcp

        Run with environment variables:
        >>> DATABASE_HOST=localhost DATABASE_NAME=mydb python -m pg_mcp
    """
    load_dotenv()  # .env -> os.environ (explicit env vars still take precedence)
    anyio.run(mcp.run_stdio_async)


if __name__ == "__main__":
    main()

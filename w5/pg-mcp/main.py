"""Main entry point for PostgreSQL MCP Server.

Runs the real pg_mcp MCP server (stdio transport). Previously this file
contained an unrelated demo server; it now delegates to pg_mcp.server so
that `python main.py` and `python -m pg_mcp` are equivalent.
"""

from dotenv import load_dotenv


def main() -> None:
    """Start the PostgreSQL MCP Server over stdio."""
    load_dotenv()  # .env -> os.environ before Settings() is built in lifespan

    import anyio

    from pg_mcp.server import mcp

    anyio.run(mcp.run_stdio_async)


if __name__ == "__main__":
    main()

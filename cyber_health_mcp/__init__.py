"""Cyber Health MCP package."""

__version__ = "0.4.1"

from .server import create_mcp_server, get_default_db_path, main

__all__ = ["create_mcp_server", "get_default_db_path", "main"]

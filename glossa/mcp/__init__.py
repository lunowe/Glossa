"""MCP (Model Context Protocol) server for Glossa.

Exposes the Glossa HTTP API as MCP tools and resources so any MCP-aware
client (Claude Desktop, Claude Code, Cursor, Zed, ...) can consult and
maintain a Glossa wiki without re-implementing the integration.

The server is stdio-transport by default and configured via env vars
(``GLOSSA_BASE_URL``, ``GLOSSA_DEFAULT_SPACE_ID``). It talks to a running
Glossa API; it does not embed Glossa itself.
"""

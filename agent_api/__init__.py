"""Agent API - REST + MCP interface for external AI agents.

This package gives local AI agents (Claude Desktop, Claude Code, Codex,
custom scripts) first-class access to Meeting Assistant:

- ``agent_api.rest``    Flask blueprint mounted at /api/agent/v1 by app.py.
- ``agent_api.helpers`` Pure formatting/conversion helpers (transcript
                        renderers, Quill Delta conversion, media probing).
- ``agent_api.context`` AgentContext: callables injected by app.py so the
                        blueprint can reach live app state without circular
                        imports.
- ``agent_api.openapi`` OpenAPI 3.1 spec builder (served at /openapi.json).

The MCP server lives at the repo root (``mcp_server.py``): a dependency-free
stdio JSON-RPC process that proxies to this REST API, meant to be spawned by
MCP clients. Human-facing documentation: docs/AGENT_API.md (also served at
GET /api/agent/v1/docs).
"""

API_VERSION = "1.0.0"

from agent_api.context import AgentContext            # noqa: E402
from agent_api.rest import bp, register_agent_api     # noqa: E402

__all__ = ["API_VERSION", "AgentContext", "bp", "register_agent_api"]

"""DramaFlow's public MCP server package.

Keep this module side-effect free so ``python -m mcp_server.server`` only builds
one server instance and does not emit protocol-breaking warnings on stdio.
"""

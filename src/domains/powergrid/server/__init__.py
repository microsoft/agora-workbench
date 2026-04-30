"""PowerGrid code execution server package."""

# Note: Do not import powergrid_server here - it pulls in server infrastructure
# (jwt, fastapi, uvicorn, mcp) which are not available in the kernel environment.
# Consumers should import directly: from domains.powergrid.server.powergrid_server import ...

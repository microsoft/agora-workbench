"""Vitrimer VAE code execution server package."""

# Note: Do not import vitrimer_vae_server here - it pulls in server infrastructure
# (jwt, fastapi, uvicorn, mcp) which are not available in the kernel environment.
# Consumers should import directly: from domains.vitrimer_vae.server.vitrimer_vae_server import ...

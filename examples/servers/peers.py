"""Shared peer topology for the example servers — a single source of truth.

Each example server passes ``peer_registry=peer_registry_for("<name>")`` into its
``ServerConfig`` so the unified ``{name}_send`` tool can push objects directly
between server kernels (e.g. ``energysystems_send(to="earthscience")``).

Keeping the map here — rather than repeating peer URLs in every server config —
is the point of the registry: operators maintain one table, not an N×N mesh of
hand-wired publishers.

These are **local-dev defaults** (each server on ``localhost`` at the port its
``docker-compose.yml`` publishes). For any other deployment (Docker Compose
internal networking, cloud), override at launch with the ``AGORA_PEER_REGISTRY``
environment variable — it takes precedence over the values baked in here. For
example, on a shared Docker network:

.. code-block:: shell

    AGORA_PEER_REGISTRY='{"earthscience":"http://earthscience-server:8000"}'

A plain-HTTP peer listed in the registry is trusted as configured — the operator
chose the scheme here, so it does not also need to appear in
``OBJECT_TRANSFER_TRUSTED_HTTP_HOSTS``. (Prefer HTTPS for non-loopback peers so
the forwarded bearer token isn't sent over an unencrypted connection.)
"""

from __future__ import annotations

# Every example server and the base URL its peers reach it at locally. The base
# URL is the server root (where ``/object-transfer/receive`` lives) — NOT the
# ``/mcp`` endpoint.
PEER_URLS: dict[str, str] = {
    "chemistry": "http://localhost:8020",
    "earthscience": "http://localhost:8021",
    "energysystems": "http://localhost:8022",
}


def peer_registry_for(server_name: str) -> dict[str, str]:
    """Return the ``peer_registry`` for *server_name*: every other example server.

    The server is excluded from its own registry (a server never sends to
    itself). ``ServerConfig`` also drops self defensively, so passing the full
    map would be harmless — excluding it here just keeps the value tidy.
    """
    return {name: url for name, url in PEER_URLS.items() if name != server_name}

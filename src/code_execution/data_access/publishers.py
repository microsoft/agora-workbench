"""
Asset publishers for pushing artifacts to remote storage destinations.

Each publisher handles artifact delivery for a specific storage backend
(Blob, local filesystem, etc.) and is the symmetric counterpart to
``AssetFetcher``.

Tag-based routing mirrors the fetcher pattern:
  - Fetching: agent passes ``<blob>abc123</blob>`` → ``BlobFetcher.can_handle()``
  - Publishing: agent passes ``<blob>results.csv</blob>`` → ``BlobPublisher.can_handle()``

Authentication:
    Publishers accept an ``AsyncTokenCredential`` (from ``azure.core``) which
    provides tokens for downstream Azure resources. In production this is
    typically backed by managed identity.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

LOGGER = logging.getLogger(__name__)


def _validate_artifact_name(name: str) -> None:
    """Validate that an artifact name is safe for path construction.

    Rejects absolute paths, parent-directory traversal (``..``), and empty
    names to prevent writes outside the intended session directory.

    Args:
        name: The logical artifact name extracted from a destination tag.

    Raises:
        ValueError: If the name is unsafe.
    """
    if not name or not name.strip():
        raise ValueError("Artifact name must not be empty.")
    if Path(name).is_absolute():
        raise ValueError(f"Artifact name must not be an absolute path: {name!r}")
    # Check for '..' in any path segment after normalization.
    normalized = name.replace("\\", "/")
    if ".." in Path(normalized).parts:
        raise ValueError(f"Artifact name must not contain parent traversal (..): {name!r}")


# Regex for parsing tag-based destination strings.
# Matches both closed (``<blob>name</blob>``) and unclosed (``<blob>name``)
# forms to tolerate LLM output that occasionally omits the closing tag.
_TAG_RE = re.compile(r"^<(\w+)>([^<>]+?)(?:</\1>)?$")


def parse_destination_tag(destination: str) -> tuple[str, str] | None:
    """Parse a tag-based destination string into (tag_type, name).

    Accepts both ``<blob>results.csv</blob>`` and ``<blob>results.csv``
    (unclosed-tag fallback for LLM robustness).

    Args:
        destination: The tagged destination string from the agent.

    Returns:
        ``(tag_type, name)`` tuple, or ``None`` if the string is not
        a recognised tag format.
    """
    m = _TAG_RE.match(destination.strip())
    if m:
        return m.group(1), m.group(2)
    return None


class AssetPublisher(ABC):
    """Base class for artifact publishers.

    Publishers are the symmetric counterpart to :class:`~.fetchers.AssetFetcher`:
    they push a local file produced by an agent session to a remote storage
    destination.

    Concrete implementations are configured at server startup and registered
    with the :class:`~code_execution.server.CodeExecutionServer`.  Operators
    control which destinations are reachable by which publishers they register —
    no separate allowlist environment variable is needed.
    """

    def __init__(self, credential: "AsyncTokenCredential | None" = None):
        """
        Initialise the publisher with an optional async token credential.

        Args:
            credential: An ``AsyncTokenCredential`` that provides tokens for
                downstream Azure resources (e.g. ``ManagedIdentityCredential``).
                May be ``None`` for publishers that don't require credentials
                (e.g. local filesystem).
        """
        self.credential = credential

    @abstractmethod
    async def publish(self, local_path: Path, name: str, session_id: str) -> str:
        """Publish a local artifact to this publisher's configured destination.

        The publisher owns path placement logic — it combines its configured
        base path with the session context and the logical name to derive the
        full destination path.

        Args:
            local_path: Absolute path to the file to publish.
            name: Logical name (relative path-like value from the tag inner
                text, e.g. ``"results.csv"`` or ``"subdir/report.pdf"``).
            session_id: Active session ID used to scope the upload path.

        Returns:
            The remote URI of the published artifact (e.g.
            ``"https://account.blob.core.windows.net/container/session/name"``
            or ``"/mnt/shared/outputs/session/name"``).
        """
        raise NotImplementedError

    @abstractmethod
    def can_handle(self, destination: str) -> bool:
        """Check whether this publisher handles the given tagged destination.

        Args:
            destination: Tagged destination string, e.g. ``"<blob>results.csv</blob>"``.

        Returns:
            ``True`` if this publisher accepts the tag type.
        """
        raise NotImplementedError

    async def close(self) -> None:
        """Release any resources held by this publisher.

        The default implementation is a no-op; override when the publisher
        holds pooled connections or clients that need explicit teardown.
        """


class BlobPublisher(AssetPublisher):
    """Publisher that uploads artifacts to Azure Blob Storage.

    Configured at startup with a storage account URL and container name.
    Files are placed at ``{container}/{session_id}/{name}`` inside the
    configured account.

    Maintains a per-account ``BlobServiceClient`` cache to amortise TCP/TLS
    handshake and token acquisition costs across multiple publishes.

    Handles destination tags of the form ``<blob>name</blob>``.
    """

    # Azure Storage scope for token acquisition
    STORAGE_SCOPE = "https://storage.azure.com/.default"

    def __init__(
        self,
        account_url: str,
        container: str,
        credential: "AsyncTokenCredential | None" = None,
    ):
        """
        Initialise the BlobPublisher.

        Args:
            account_url: Azure Storage account URL, e.g.
                ``"https://myaccount.blob.core.windows.net"``.
            container: Container name to upload into.
            credential: An ``AsyncTokenCredential`` for blob auth (typically
                managed identity).  Reuse the same credential instance as the
                server's :class:`~.fetchers.BlobFetcher` to avoid redundant
                token refreshes.
        """
        super().__init__(credential=credential)
        self._account_url = account_url.rstrip("/")
        self._container = container
        self._client = None  # lazily initialised

    def _get_client(self):
        """Return (or lazily create) the BlobServiceClient."""
        if self._client is None:
            from azure.storage.blob.aio import BlobServiceClient

            self._client = BlobServiceClient(
                account_url=self._account_url,
                credential=self.credential,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying BlobServiceClient."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<blob>…</blob>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "blob"

    async def publish(self, local_path: Path, name: str, session_id: str) -> str:
        """Upload *local_path* to ``{container}/{session_id}/{name}``.

        Args:
            local_path: Absolute path to the file to upload.
            name: Logical name / relative path within the session's namespace.
            session_id: Session ID used to scope the blob path.

        Returns:
            The full HTTPS URL of the uploaded blob.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            azure.core.exceptions.ClientAuthenticationError: If the credential
                is not authorised to write to the container.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")

        _validate_artifact_name(name)

        blob_path = f"{session_id}/{name}"
        LOGGER.info(
            "BlobPublisher: uploading %s → %s/%s/%s",
            local_path,
            self._account_url,
            self._container,
            blob_path,
        )

        client = self._get_client()
        blob_client = client.get_blob_client(container=self._container, blob=blob_path)

        with open(local_path, "rb") as fh:
            await blob_client.upload_blob(fh, overwrite=True)

        remote_uri = f"{self._account_url}/{self._container}/{blob_path}"
        LOGGER.info("BlobPublisher: uploaded %d bytes → %s", local_path.stat().st_size, remote_uri)
        return remote_uri


class GuiPublisher(AssetPublisher):
    """Publisher that exposes artifacts via the server's download endpoint.

    Unlike Blob or Local publishers that transfer the file elsewhere, this
    publisher simply returns the server's ``/artifacts/`` download URL for
    the already-registered artifact.  The activity UI surfaces this URL so
    the user can download the file from their browser.

    This publisher is auto-registered on every ``CodeExecutionServer`` so
    agents can always use ``<gui>filename</gui>`` to make outputs
    downloadable without requiring operator-configured storage backends.

    Handles destination tags of the form ``<gui>name</gui>``.
    """

    def __init__(self, public_url_fn: "Callable[[], str]"):
        """
        Initialise the GuiPublisher.

        Args:
            public_url_fn: A callable returning the server's public base URL
                (e.g. ``server.public_url``).  Deferred so the URL reflects
                the actual bind address after ``run_http()`` is called.
        """
        super().__init__(credential=None)
        self._public_url_fn = public_url_fn

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<gui>…</gui>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "gui"

    async def publish(self, local_path: Path, name: str, session_id: str) -> str:
        """Return the download URL for the artifact.

        The artifact must already be registered in the session manager's
        artifact registry (populated by the snapshot-diff after execution).
        The caller (the publish tool wrapper) is responsible for passing the
        download token via the ``_download_token`` attribute set on this
        instance before calling ``publish()``.

        Args:
            local_path: Absolute path to the artifact file.
            name: Logical name / relative path for the URL's filename segment.
            session_id: Session ID scoping the artifact.

        Returns:
            The fully-qualified download URL.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
            RuntimeError: If no download token was provided.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")

        _validate_artifact_name(name)

        token = getattr(self, "_download_token", None)
        if not token:
            raise RuntimeError(
                "GuiPublisher requires a download token set via _download_token before publish() is called."
            )

        public_base = (os.getenv("SERVER_PUBLIC_URL") or self._public_url_fn()).rstrip("/")
        download_url = f"{public_base}/artifacts/{session_id}/{token}/{name}"
        LOGGER.info("GuiPublisher: exposing %s → %s", local_path, download_url)
        return download_url


class LocalFilePublisher(AssetPublisher):
    """Publisher that copies artifacts to a local directory.

    Configured at startup with a base directory.  Files are placed at
    ``{base_dir}/{session_id}/{name}``.

    Handles destination tags of the form ``<local>name</local>``.
    """

    def __init__(self, base_dir: Path | str):
        """
        Initialise the LocalFilePublisher.

        Args:
            base_dir: Base directory under which session sub-directories are
                created.  Must be an absolute path (or will be resolved to
                one).
        """
        super().__init__(credential=None)
        self._base_dir = Path(base_dir).resolve()

    def can_handle(self, destination: str) -> bool:
        """Return ``True`` for ``<local>…</local>`` destinations."""
        parsed = parse_destination_tag(destination)
        return parsed is not None and parsed[0] == "local"

    async def publish(self, local_path: Path, name: str, session_id: str) -> str:
        """Copy *local_path* to ``{base_dir}/{session_id}/{name}``.

        Args:
            local_path: Absolute path to the file to copy.
            name: Logical name / relative path within the session's namespace.
            session_id: Session ID used to scope the destination path.

        Returns:
            The absolute path of the copied file as a string.

        Raises:
            FileNotFoundError: If *local_path* does not exist.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Artifact not found at {local_path}")

        _validate_artifact_name(name)

        dest = (self._base_dir / session_id / name).resolve()
        # Belt-and-suspenders: verify the resolved path is still within the
        # expected session directory even after symlink resolution.
        session_root = (self._base_dir / session_id).resolve()
        if not dest.is_relative_to(session_root):
            raise ValueError(f"Resolved artifact path {dest} escapes the session directory {session_root}.")
        dest.parent.mkdir(parents=True, exist_ok=True)

        LOGGER.info("LocalFilePublisher: copying %s → %s", local_path, dest)
        shutil.copy2(local_path, dest)
        LOGGER.info("LocalFilePublisher: copied %d bytes → %s", dest.stat().st_size, dest)
        return str(dest)

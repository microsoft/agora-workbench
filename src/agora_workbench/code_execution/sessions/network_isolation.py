"""Linux network-namespace isolation for Jupyter kernels."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jupyter_client.provisioning.factory import KernelProvisionerFactory

if TYPE_CHECKING:
    from jupyter_client.manager import AsyncKernelManager


_UNSHARE_ARGS = ("--user", "--map-current-user", "--net", "--")
_SETPRIV_ARGS = (
    "--no-new-privs",
    "--bounding-set=-all",
    "--inh-caps=-all",
    "--ambient-caps=-all",
    "--",
)
_MAX_UNIX_SOCKET_PATH_BYTES = 107


def _required_executable(name: str, purpose: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"isolated kernel networking requires {purpose} ('{name}')")
    return executable


def _isolation_prefix() -> list[str]:
    if sys.platform != "linux":
        raise RuntimeError("isolated kernel networking is supported only on Linux")

    unshare = _required_executable("unshare", "the util-linux unshare executable")
    setpriv = _required_executable("setpriv", "the util-linux setpriv executable")
    ip = _required_executable("ip", "the iproute2 ip executable")
    return [
        unshare,
        *_UNSHARE_ARGS,
        sys.executable,
        str(Path(__file__).resolve()),
        "--ip",
        ip,
        "--setpriv",
        setpriv,
        "--",
    ]


def _namespace_launcher(kernel_argv: list[str], *, ip: str, setpriv: str) -> None:
    """Prepare the private netns, drop privileges, and exec the kernel."""
    if not kernel_argv:
        raise RuntimeError("isolated kernel networking requires a non-empty kernel command")

    try:
        subprocess.run(
            [ip, "link", "set", "lo", "up"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise RuntimeError(f"failed to enable private kernel loopback: {detail}") from exc

    os.execv(setpriv, [setpriv, *_SETPRIV_ARGS, *kernel_argv])


def _normalized_kernel_argv(kernel_manager: "AsyncKernelManager") -> list[str]:
    kernel_spec = kernel_manager.kernel_spec
    if kernel_spec is None:
        raise RuntimeError("isolated kernel networking requires a local kernelspec")

    metadata = kernel_spec.metadata or {}
    provisioner = metadata.get("kernel_provisioner", {})
    if not isinstance(provisioner, dict):
        raise RuntimeError("isolated kernel networking requires valid kernel_provisioner metadata")
    provisioner_name = provisioner.get(
        "provisioner_name",
        KernelProvisionerFactory.instance().default_provisioner_name,
    )
    if provisioner_name != "local-provisioner":
        raise RuntimeError(
            "isolated kernel networking supports only the Jupyter local-provisioner; "
            f"configured provisioner is {provisioner_name!r}"
        )

    argv = list(kernel_spec.argv)
    if not argv:
        raise RuntimeError("isolated kernel networking requires a non-empty kernelspec argv")
    if argv[0] in {
        "python",
        f"python{sys.version_info.major}",
        f"python{sys.version_info.major}.{sys.version_info.minor}",
    }:
        argv[0] = sys.executable
    return argv


def validate_network_isolation_support() -> None:
    """Fail closed unless this process can create the required namespaces."""
    prefix = _isolation_prefix()
    true = _required_executable("true", "a POSIX true executable")
    try:
        result = subprocess.run(
            [*prefix, true],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to validate isolated kernel networking: {exc}") from exc
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(
        "isolated kernel networking is unavailable; the runtime must permit "
        f"unprivileged user and network namespaces{suffix}"
    )


def configure_isolated_kernel(kernel_manager: "AsyncKernelManager") -> Path:
    """Use IPC for Jupyter channels and launch the kernel in an empty netns."""
    ipc_dir = Path(tempfile.mkdtemp(prefix="agora-kernel-ipc-"))
    try:
        kernel_argv = _normalized_kernel_argv(kernel_manager)
        ipc_prefix = ipc_dir / "kernel"
        longest_socket_path = f"{ipc_prefix}-65535"
        if len(os.fsencode(longest_socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise RuntimeError(f"isolated kernel IPC path is too long for a Unix-domain socket: {longest_socket_path}")
        kernel_manager.transport = "ipc"
        kernel_manager.ip = str(ipc_prefix)
        kernel_manager.kernel_spec.argv = [
            *_isolation_prefix(),
            *kernel_argv,
        ]
    except BaseException:
        shutil.rmtree(ipc_dir, ignore_errors=True)
        raise
    return ipc_dir


def cleanup_isolated_kernel(ipc_dir: Path | None) -> None:
    """Remove IPC socket files and their private directory."""
    if ipc_dir is not None:
        shutil.rmtree(ipc_dir, ignore_errors=True)


if __name__ == "__main__":
    launcher_args = sys.argv[1:]
    if (
        len(launcher_args) < 6
        or launcher_args[0] != "--ip"
        or launcher_args[2] != "--setpriv"
        or launcher_args[4] != "--"
    ):
        raise SystemExit("network-isolation launcher requires '--ip <path> --setpriv <path> -- <kernel command>'")
    _namespace_launcher(
        launcher_args[5:],
        ip=launcher_args[1],
        setpriv=launcher_args[3],
    )

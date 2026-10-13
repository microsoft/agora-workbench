"""Linux network isolation backends for Jupyter kernels."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from jupyter_client.provisioning.factory import KernelProvisionerFactory

if TYPE_CHECKING:
    from jupyter_client.asynchronous.client import AsyncKernelClient
    from jupyter_client.manager import AsyncKernelManager


_UNSHARE_ARGS = ("--user", "--map-root-user", "--net", "--")
_SETPRIV_ARGS = (
    "--no-new-privs",
    "--bounding-set=-all",
    "--inh-caps=-all",
    "--ambient-caps=-all",
    "--",
)
_MAX_UNIX_SOCKET_PATH_BYTES = 107
_SECCOMP_BOOTSTRAP_TIMEOUT_SECONDS = 30
_SECCOMP_INSTALL_CODE = textwrap.dedent(
    """
    def __agora_install_seccomp_network_filter():
        import ctypes
        import errno
        import os
        import socket

        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("seccomp kernel networking requires a 64-bit Linux process")

        class ScmpArgCmp(ctypes.Structure):
            _fields_ = [
                ("arg", ctypes.c_uint),
                ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64),
                ("datum_b", ctypes.c_uint64),
            ]

        seccomp = None
        load_errors = []
        for library_name in ("libseccomp.so.2", "libseccomp.so"):
            try:
                seccomp = ctypes.CDLL(library_name, use_errno=True)
                break
            except OSError as exc:
                load_errors.append(str(exc))
        if seccomp is None:
            detail = "; ".join(load_errors)
            raise RuntimeError(
                "seccomp kernel networking requires the libseccomp shared library"
                + (f": {detail}" if detail else "")
            )

        seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        seccomp.seccomp_init.restype = ctypes.c_void_p
        seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_release.restype = None
        seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        seccomp.seccomp_attr_set.restype = ctypes.c_int
        seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        seccomp.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(ScmpArgCmp),
        ]
        seccomp.seccomp_rule_add_array.restype = ctypes.c_int
        seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_load.restype = ctypes.c_int

        pr_set_no_new_privs = 38
        scmp_fltatr_ctl_tsync = 4
        scmp_cmp_ne = 1

        def check_result(result, operation):
            if result != 0:
                error_number = -result if result < 0 else result
                raise OSError(error_number, f"{operation} failed: {errno.errorcode.get(error_number, error_number)}")

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(pr_set_no_new_privs, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, "failed to set no_new_privs before loading the seccomp filter")

        status = {}
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                if ":" in line:
                    name, value = line.split(":", 1)
                    status[name] = value.strip()
        retained_capabilities = {
            name: status.get(name, "0")
            for name in ("CapInh", "CapPrm", "CapEff", "CapAmb")
            if int(status.get(name, "0"), 16) != 0
        }
        if retained_capabilities:
            raise RuntimeError(
                "seccomp kernel networking requires a process without inherited capabilities: "
                f"{retained_capabilities}"
            )

        allow = 0x7FFF0000
        deny = 0x00050000 | errno.EPERM
        context = seccomp.seccomp_init(allow)
        if not context:
            raise RuntimeError("libseccomp failed to allocate a filter context")

        try:
            # Apply the filter to every existing kernel thread, not only the
            # thread that handles this bootstrap execution.
            check_result(
                seccomp.seccomp_attr_set(context, scmp_fltatr_ctl_tsync, 1),
                "enabling seccomp thread synchronization",
            )

            socket_syscall = seccomp.seccomp_syscall_resolve_name(b"socket")
            if socket_syscall < 0:
                raise RuntimeError("libseccomp could not resolve the socket syscall")
            comparison = ScmpArgCmp(0, scmp_cmp_ne, socket.AF_UNIX, 0)
            check_result(
                seccomp.seccomp_rule_add_array(context, deny, socket_syscall, 1, ctypes.pointer(comparison)),
                "adding the socket-domain rule",
            )
            socketpair_syscall = seccomp.seccomp_syscall_resolve_name(b"socketpair")
            if socketpair_syscall >= 0:
                check_result(
                    seccomp.seccomp_rule_add_array(
                        context,
                        deny,
                        socketpair_syscall,
                        1,
                        ctypes.pointer(comparison),
                    ),
                    "adding the socketpair-domain rule",
                )

            # The Jupyter IPC channels are already connected. Denying future
            # connect() calls prevents one same-UID kernel from attaching to a
            # different kernel's startup sockets before its filter is loaded.
            connect_syscall = seccomp.seccomp_syscall_resolve_name(b"connect")
            if connect_syscall >= 0:
                check_result(
                    seccomp.seccomp_rule_add_array(context, deny, connect_syscall, 0, None),
                    "adding the connect rule",
                )

            # A 64-bit process normally uses socket(), but deny the legacy
            # multiplexed syscall too when the architecture exposes it.
            socketcall_syscall = seccomp.seccomp_syscall_resolve_name(b"socketcall")
            if socketcall_syscall >= 0:
                check_result(
                    seccomp.seccomp_rule_add_array(context, deny, socketcall_syscall, 0, None),
                    "adding the socketcall rule",
                )

            # Blocking io_uring creation prevents applications from bypassing
            # the socket syscall rule through asynchronous networking opcodes.
            for syscall_name in (b"io_uring_setup", b"io_uring_enter", b"io_uring_register"):
                syscall_number = seccomp.seccomp_syscall_resolve_name(syscall_name)
                if syscall_number >= 0:
                    check_result(
                        seccomp.seccomp_rule_add_array(context, deny, syscall_number, 0, None),
                        f"adding the {syscall_name.decode()} rule",
                    )

            check_result(seccomp.seccomp_load(context), "loading the seccomp filter")
        finally:
            seccomp.seccomp_release(context)

        unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix_socket.close()
        socketpair_left, socketpair_right = socket.socketpair()
        socketpair_left.close()
        socketpair_right.close()
        connect_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connect_socket.connect("/nonexistent-agora-seccomp-verification.sock")
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise RuntimeError(
                    f"seccomp verification returned errno {exc.errno} instead of EPERM for connect"
                ) from exc
        else:
            raise RuntimeError("seccomp verification unexpectedly allowed a new connection")
        finally:
            connect_socket.close()

        for fd_name in os.listdir("/proc/self/fd"):
            try:
                fd = int(fd_name)
                duplicate = os.dup(fd)
            except (OSError, ValueError):
                continue
            try:
                try:
                    inherited_socket = socket.socket(fileno=duplicate)
                except OSError:
                    os.close(duplicate)
                    continue
                try:
                    family = inherited_socket.family
                finally:
                    inherited_socket.close()
            except BaseException:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
                raise
            if family != socket.AF_UNIX:
                raise RuntimeError(f"seccomp bootstrap found an existing non-Unix socket on file descriptor {fd}")

        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                ip_socket = socket.socket(family, socket.SOCK_STREAM)
            except OSError as exc:
                if exc.errno != errno.EPERM:
                    raise RuntimeError(
                        f"seccomp verification returned errno {exc.errno} instead of EPERM for family {family}"
                    ) from exc
            else:
                ip_socket.close()
                raise RuntimeError(f"seccomp verification unexpectedly created an IP socket for family {family}")

    __agora_install_seccomp_network_filter()
    del __agora_install_seccomp_network_filter
    """
).strip()


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


def validate_seccomp_isolation_support() -> None:
    """Fail closed unless a child process can load the required seccomp filter."""
    if sys.platform != "linux":
        raise RuntimeError("seccomp kernel networking is supported only on Linux")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _SECCOMP_INSTALL_CODE],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to validate seccomp kernel networking: {exc}") from exc
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout).strip()
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"seccomp kernel networking is unavailable{suffix}")


def _kernel_python_executable(kernel_manager: "AsyncKernelManager") -> Path:
    kernel_argv = _normalized_kernel_argv(kernel_manager)
    executable_name = kernel_argv[0]
    executable_path = Path(executable_name)
    if not executable_path.is_absolute():
        resolved_executable = shutil.which(executable_name)
        if resolved_executable is None:
            raise RuntimeError(f"seccomp kernel networking could not resolve kernel executable {executable_name!r}")
        executable_path = Path(resolved_executable)
    return executable_path.absolute()


def _seccomp_kernel_startup_paths(kernel_manager: "AsyncKernelManager") -> dict[str, Path]:
    executable_path = _kernel_python_executable(kernel_manager)
    probe_code = (
        "import json,sys,sysconfig;"
        "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
        "'paths':{name:sysconfig.get_path(name) "
        "for name in ('stdlib','platstdlib','purelib','platlib')}}))"
    )
    try:
        result = subprocess.run(
            [str(executable_path), "-I", "-S", "-c", probe_code],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to inspect the seccomp kernel Python environment: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"seccomp kernel networking requires a Python kernelspec{suffix}")

    try:
        python_metadata = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("seccomp kernel networking could not inspect the kernel Python paths") from exc
    if not isinstance(python_metadata, dict):
        raise RuntimeError("seccomp kernel networking received invalid kernel Python path metadata")

    version = python_metadata.get("version")
    python_paths = python_metadata.get("paths")
    if (
        not isinstance(version, list)
        or len(version) != 2
        or not all(isinstance(component, int) for component in version)
        or not isinstance(python_paths, dict)
    ):
        raise RuntimeError("seccomp kernel networking received invalid kernel Python path metadata")

    startup_paths: dict[str, Path] = {
        "kernel executable": executable_path,
        "kernel executable directory": executable_path.parent,
        "resolved kernel executable": executable_path.resolve(),
        "resolved kernel executable directory": executable_path.resolve().parent,
    }
    for name, path in python_paths.items():
        if isinstance(name, str) and isinstance(path, str) and path:
            startup_paths[name] = Path(path).absolute()

    venv_root = executable_path.parent.parent
    pyvenv_config = venv_root / "pyvenv.cfg"
    if pyvenv_config.exists():
        major, minor = version
        startup_paths["pyvenv configuration"] = pyvenv_config
        venv_site_packages = venv_root / "lib" / f"python{major}.{minor}" / "site-packages"
        startup_paths["venv site-packages"] = venv_site_packages
        lib64_site_packages = venv_root / "lib64" / f"python{major}.{minor}" / "site-packages"
        if lib64_site_packages.exists():
            startup_paths["venv lib64 site-packages"] = lib64_site_packages

    for label, path in tuple(startup_paths.items()):
        resolved_path = path.resolve()
        if resolved_path != path:
            startup_paths[f"resolved {label}"] = resolved_path
    return startup_paths


def _path_and_parents(path: Path) -> list[Path]:
    paths = [path]
    while paths[-1].parent != paths[-1]:
        paths.append(paths[-1].parent)
    return paths


def validate_seccomp_kernel_environment(kernel_manager: "AsyncKernelManager") -> None:
    """Require every startup path to have an immutable ownership chain."""
    if os.geteuid() == 0:
        raise RuntimeError("seccomp kernel networking requires Workbench to run as a non-root user")

    startup_paths = _seccomp_kernel_startup_paths(kernel_manager)
    kernel_uid = os.geteuid()
    unsafe_paths: list[str] = []
    inspected_paths: set[Path] = set()
    for label, path in startup_paths.items():
        for controlled_path in _path_and_parents(path):
            if controlled_path in inspected_paths:
                continue
            inspected_paths.add(controlled_path)
            try:
                stat_result = controlled_path.stat()
            except OSError as exc:
                raise RuntimeError(
                    f"seccomp kernel networking could not inspect {label} path {controlled_path}: {exc}"
                ) from exc
            if stat_result.st_uid == kernel_uid or os.access(controlled_path, os.W_OK):
                unsafe_paths.append(f"{controlled_path} (controls {label})")
    if unsafe_paths:
        detail = ", ".join(unsafe_paths)
        raise RuntimeError(
            "seccomp kernel networking requires an immutable Python environment owned by another operating-system "
            f"user; kernel code can modify or replace: {detail}"
        )


def _configure_ipc_kernel(kernel_manager: "AsyncKernelManager") -> tuple[Path, list[str]]:
    ipc_dir = Path(tempfile.mkdtemp(prefix="agora-kernel-ipc-"))
    try:
        kernel_argv = _normalized_kernel_argv(kernel_manager)
        ipc_prefix = ipc_dir / "kernel"
        longest_socket_path = f"{ipc_prefix}-65535"
        if len(os.fsencode(longest_socket_path)) > _MAX_UNIX_SOCKET_PATH_BYTES:
            raise RuntimeError(f"isolated kernel IPC path is too long for a Unix-domain socket: {longest_socket_path}")
        kernel_manager.transport = "ipc"
        kernel_manager.ip = str(ipc_prefix)
    except BaseException:
        shutil.rmtree(ipc_dir, ignore_errors=True)
        raise
    return ipc_dir, kernel_argv


def configure_isolated_kernel(kernel_manager: "AsyncKernelManager") -> Path:
    """Use IPC for Jupyter channels and launch the kernel in an empty netns."""
    ipc_dir, kernel_argv = _configure_ipc_kernel(kernel_manager)
    try:
        kernel_manager.kernel_spec.argv = [
            *_isolation_prefix(),
            *kernel_argv,
        ]
    except BaseException:
        shutil.rmtree(ipc_dir, ignore_errors=True)
        raise
    return ipc_dir


def configure_seccomp_kernel(kernel_manager: "AsyncKernelManager") -> Path:
    """Use IPC for Jupyter channels before installing a process-local filter."""
    executable_path = _kernel_python_executable(kernel_manager)
    ipc_dir, _ = _configure_ipc_kernel(kernel_manager)
    try:
        kernel_manager.kernel_spec.argv = [
            str(executable_path),
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
        ]
        kernel_manager.kernel_spec.env = {}
    except BaseException:
        shutil.rmtree(ipc_dir, ignore_errors=True)
        raise
    return ipc_dir


async def install_seccomp_network_filter(
    kernel_client: "AsyncKernelClient",
    working_dir: str,
) -> None:
    """Install and verify the filter before exposing the kernel to callers."""
    bootstrap_code = (
        f"{_SECCOMP_INSTALL_CODE}\nimport os as __agora_os\n__agora_os.chdir({working_dir!r})\ndel __agora_os\n"
    )
    msg_id = kernel_client.execute(
        bootstrap_code,
        silent=True,
        store_history=False,
        allow_stdin=False,
        stop_on_error=True,
    )
    try:
        reply = await kernel_client.get_shell_msg(timeout=_SECCOMP_BOOTSTRAP_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, queue.Empty) as exc:
        raise RuntimeError("timed out while installing the seccomp kernel network filter") from exc

    while reply.get("parent_header", {}).get("msg_id") != msg_id:
        try:
            reply = await kernel_client.get_shell_msg(timeout=_SECCOMP_BOOTSTRAP_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, queue.Empty) as exc:
            raise RuntimeError("timed out while waiting for the seccomp kernel bootstrap reply") from exc

    content = reply.get("content", {})
    if content.get("status") != "ok":
        error_name = content.get("ename", "KernelError")
        error_value = content.get("evalue", "unknown error")
        raise RuntimeError(f"failed to install the seccomp kernel network filter: {error_name}: {error_value}")

    while True:
        try:
            message = await kernel_client.get_iopub_msg(timeout=_SECCOMP_BOOTSTRAP_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, queue.Empty) as exc:
            raise RuntimeError("timed out while waiting for the seccomp kernel bootstrap to become idle") from exc
        if message.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        if message.get("msg_type") == "status" and message.get("content", {}).get("execution_state") == "idle":
            return


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

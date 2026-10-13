"""Linux network isolation backends for Jupyter kernels."""

from __future__ import annotations

import asyncio
import ctypes
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
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
_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4
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
            if socketpair_syscall < 0:
                raise RuntimeError("libseccomp could not resolve the required socketpair syscall")
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
            if connect_syscall < 0:
                raise RuntimeError("libseccomp could not resolve the required connect syscall")
            check_result(
                seccomp.seccomp_rule_add_array(context, deny, connect_syscall, 0, None),
                "adding the connect rule",
            )

            # All kernel-owned listeners are established before the filter.
            # Prevent user code from creating a Unix-socket fd-import channel.
            for syscall_name in (b"bind", b"listen"):
                syscall_number = seccomp.seccomp_syscall_resolve_name(syscall_name)
                if syscall_number < 0:
                    raise RuntimeError(
                        f"libseccomp could not resolve the required {syscall_name.decode()} syscall"
                    )
                check_result(
                    seccomp.seccomp_rule_add_array(context, deny, syscall_number, 0, None),
                    f"adding the {syscall_name.decode()} rule",
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
                if syscall_number < 0:
                    raise RuntimeError(
                        f"libseccomp could not resolve the required {syscall_name.decode()} syscall"
                    )
                check_result(
                    seccomp.seccomp_rule_add_array(context, deny, syscall_number, 0, None),
                    f"adding the {syscall_name.decode()} rule",
                )

            # Same-UID process injection would otherwise let a filtered kernel
            # execute inside the unfiltered server or a starting sibling kernel.
            for syscall_name in (b"ptrace", b"process_vm_readv", b"process_vm_writev", b"pidfd_getfd"):
                syscall_number = seccomp.seccomp_syscall_resolve_name(syscall_name)
                if syscall_number < 0:
                    raise RuntimeError(
                        f"libseccomp could not resolve the required {syscall_name.decode()} syscall"
                    )
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


@dataclass(frozen=True)
class SeccompKernelEnvironment:
    """Validated paths required to start a seccomp-isolated Python kernel."""

    executable: Path
    import_paths: tuple[Path, ...]


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


def _disable_process_dumpability() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "failed to disable process dumpability for seccomp kernel isolation")
    if libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise RuntimeError("seccomp kernel networking could not disable process dumpability")


def validate_seccomp_isolation_support() -> None:
    """Fail closed unless a child process can load the required seccomp filter."""
    if sys.platform != "linux":
        raise RuntimeError("seccomp kernel networking is supported only on Linux")
    _disable_process_dumpability()
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
    allowed_arguments = {
        ("-m", "ipykernel_launcher", "-f", "{connection_file}"),
        ("-Xfrozen_modules=off", "-m", "ipykernel_launcher", "-f", "{connection_file}"),
    }
    if tuple(kernel_argv[1:]) not in allowed_arguments:
        raise RuntimeError(
            "seccomp kernel networking requires a standard Python ipykernel kernelspec; "
            f"configured argv is {kernel_argv!r}"
        )
    kernel_spec = kernel_manager.kernel_spec
    if kernel_spec is None:
        raise RuntimeError("seccomp kernel networking requires a local kernelspec")
    if kernel_spec.env:
        raise RuntimeError(
            "seccomp kernel networking does not support kernelspec environment entries; "
            f"configured variables are {sorted(kernel_spec.env)}"
        )
    executable_name = kernel_argv[0]
    executable_path = Path(executable_name)
    if not executable_path.is_absolute():
        resolved_executable = shutil.which(executable_name)
        if resolved_executable is None:
            raise RuntimeError(f"seccomp kernel networking could not resolve kernel executable {executable_name!r}")
        executable_path = Path(resolved_executable)
    return executable_path.absolute()


def _with_resolved_paths(startup_paths: dict[str, Path]) -> dict[str, Path]:
    resolved_paths = dict(startup_paths)
    for label, path in startup_paths.items():
        resolved_path = path.resolve()
        if resolved_path != path:
            resolved_paths[f"resolved {label}"] = resolved_path
    return resolved_paths


def _seccomp_kernel_initial_paths(executable_path: Path) -> dict[str, Path]:
    startup_paths = {
        "kernel executable": executable_path,
        "kernel executable directory": executable_path.parent,
        "resolved kernel executable": executable_path.resolve(),
        "resolved kernel executable directory": executable_path.resolve().parent,
    }
    pyvenv_config = executable_path.parent.parent / "pyvenv.cfg"
    if pyvenv_config.exists():
        startup_paths["pyvenv configuration"] = pyvenv_config
    return _with_resolved_paths(startup_paths)


def _read_pyvenv_config(pyvenv_config: Path) -> dict[str, str]:
    try:
        lines = pyvenv_config.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"seccomp kernel networking could not read {pyvenv_config}: {exc}") from exc

    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    return values


def _python_version_from_environment(
    executable_path: Path,
    environment_root: Path,
    pyvenv_values: dict[str, str],
) -> tuple[int, int]:
    version_value = pyvenv_values.get("version", "")
    version_match = re.match(r"^(?P<major>\d+)\.(?P<minor>\d+)", version_value)
    if version_match is not None:
        return int(version_match.group("major")), int(version_match.group("minor"))

    executable_match = re.search(r"python(?P<major>\d+)\.(?P<minor>\d+)", executable_path.resolve().name)
    if executable_match is not None:
        return int(executable_match.group("major")), int(executable_match.group("minor"))

    versions: set[tuple[int, int]] = set()
    for library_directory in (environment_root / "lib", environment_root / "lib64"):
        if not library_directory.is_dir():
            continue
        for candidate in library_directory.glob("python*.*"):
            candidate_match = re.fullmatch(r"python(?P<major>\d+)\.(?P<minor>\d+)", candidate.name)
            if candidate_match is not None and candidate.is_dir():
                versions.add((int(candidate_match.group("major")), int(candidate_match.group("minor"))))
    if len(versions) == 1:
        return next(iter(versions))
    raise RuntimeError(
        "seccomp kernel networking could not determine the kernel Python version from its immutable environment"
    )


def _python_prefix_from_pyvenv(environment_root: Path, pyvenv_values: dict[str, str]) -> Path:
    configured_executable = pyvenv_values.get("executable")
    if configured_executable:
        executable_path = Path(configured_executable)
        if not executable_path.is_absolute():
            executable_path = environment_root / executable_path
        return executable_path.absolute().parent.parent

    configured_home = pyvenv_values.get("home")
    if configured_home:
        home_path = Path(configured_home)
        if not home_path.is_absolute():
            home_path = environment_root / home_path
        home_path = home_path.absolute()
        return home_path.parent if home_path.name in {"bin", "Scripts"} else home_path

    raise RuntimeError("seccomp kernel networking requires pyvenv.cfg to declare 'home' or 'executable'")


def _python_standard_library_paths(prefix: Path, version: tuple[int, int]) -> tuple[Path, ...]:
    major, minor = version
    candidates = [
        prefix / "lib" / f"python{major}.{minor}",
        prefix / "lib64" / f"python{major}.{minor}",
        prefix / "lib" / f"python{major}{minor}.zip",
        prefix / "lib64" / f"python{major}{minor}.zip",
    ]
    return tuple(path for path in candidates if path.exists())


def _python_package_paths(prefix: Path, version: tuple[int, int]) -> tuple[Path, ...]:
    major, minor = version
    candidates = [
        prefix / "lib" / f"python{major}.{minor}" / "site-packages",
        prefix / "lib64" / f"python{major}.{minor}" / "site-packages",
        prefix / "lib" / f"python{major}.{minor}" / "dist-packages",
        prefix / "lib64" / f"python{major}.{minor}" / "dist-packages",
        prefix / "lib" / f"python{major}" / "dist-packages",
        prefix / "lib64" / f"python{major}" / "dist-packages",
    ]
    return tuple(path for path in candidates if path.is_dir())


def _seccomp_kernel_startup_paths(
    executable_path: Path,
) -> tuple[dict[str, Path], tuple[Path, ...], tuple[Path, ...]]:
    startup_paths = _seccomp_kernel_initial_paths(executable_path)
    venv_root = executable_path.parent.parent
    pyvenv_config = venv_root / "pyvenv.cfg"
    pyvenv_values: dict[str, str] = {}
    if pyvenv_config.exists():
        pyvenv_values = _read_pyvenv_config(pyvenv_config)

    version = _python_version_from_environment(executable_path, venv_root, pyvenv_values)
    base_prefix = _python_prefix_from_pyvenv(venv_root, pyvenv_values) if pyvenv_values else venv_root
    if pyvenv_values:
        configured_home = pyvenv_values.get("home")
        if configured_home:
            home_path = Path(configured_home)
            startup_paths["pyvenv home"] = (home_path if home_path.is_absolute() else venv_root / home_path).absolute()
        configured_executable = pyvenv_values.get("executable")
        if configured_executable:
            configured_executable_path = Path(configured_executable)
            startup_paths["pyvenv executable"] = (
                configured_executable_path
                if configured_executable_path.is_absolute()
                else venv_root / configured_executable_path
            ).absolute()

    standard_library_paths = _python_standard_library_paths(base_prefix, version)
    if not standard_library_paths:
        raise RuntimeError(
            f"seccomp kernel networking could not locate the kernel Python standard library under {base_prefix}"
        )
    for index, path in enumerate(standard_library_paths):
        startup_paths[f"standard library {index + 1}"] = path

    import_paths = list(_python_package_paths(venv_root, version))
    include_system_site_packages = pyvenv_values.get("include-system-site-packages", "").lower() == "true"
    if not pyvenv_values or include_system_site_packages:
        import_paths.extend(_python_package_paths(base_prefix, version))
    import_paths = list(dict.fromkeys(import_paths))
    if not any((path / "ipykernel").is_dir() or (path / "ipykernel.py").is_file() for path in import_paths):
        raise RuntimeError("seccomp kernel networking could not locate ipykernel in the immutable Python environment")
    for index, path in enumerate(import_paths):
        startup_paths[f"package root {index + 1}"] = path

    startup_tree_paths = tuple(dict.fromkeys((*standard_library_paths, *import_paths)))
    return _with_resolved_paths(startup_paths), tuple(import_paths), startup_tree_paths


def _path_and_parents(path: Path) -> list[Path]:
    paths = [path]
    while paths[-1].parent != paths[-1]:
        paths.append(paths[-1].parent)
    return paths


def _validate_seccomp_startup_paths(
    startup_paths: dict[str, Path],
    *,
    kernel_uid: int,
    inspected_paths: set[Path] | None = None,
) -> set[Path]:
    inspected_paths = inspected_paths or set()
    unsafe_paths: list[str] = []
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
            if _path_is_mutable_by_kernel(controlled_path, stat_result, kernel_uid):
                unsafe_paths.append(f"{controlled_path} (controls {label})")
    if unsafe_paths:
        detail = ", ".join(unsafe_paths)
        raise RuntimeError(
            "seccomp kernel networking requires an immutable Python environment owned by another operating-system "
            f"user; kernel code can modify or replace: {detail}"
        )
    return inspected_paths


def _path_is_mutable_by_kernel(path: Path, stat_result: os.stat_result, kernel_uid: int) -> bool:
    return stat_result.st_uid == kernel_uid or os.access(path, os.W_OK)


def _validate_seccomp_startup_trees(
    startup_paths: tuple[Path, ...],
    *,
    kernel_uid: int,
    inspected_paths: set[Path],
) -> None:
    pending = [(path, str(path)) for path in startup_paths]
    visited_directories: set[tuple[int, int]] = set()
    while pending:
        path, label = pending.pop()
        try:
            stat_result = path.lstat()
        except OSError as exc:
            raise RuntimeError(f"seccomp kernel networking could not inspect startup path {path}: {exc}") from exc
        if _path_is_mutable_by_kernel(path, stat_result, kernel_uid):
            raise RuntimeError(
                "seccomp kernel networking requires an immutable Python environment owned by another "
                f"operating-system user; kernel code can modify or replace: {path} (within {label})"
            )

        if stat.S_ISLNK(stat_result.st_mode):
            try:
                resolved_path = path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    f"seccomp kernel networking could not resolve startup symlink {path}: {exc}"
                ) from exc
            _validate_seccomp_startup_paths(
                {f"symlink target for {path}": resolved_path},
                kernel_uid=kernel_uid,
                inspected_paths=inspected_paths,
            )
            pending.append((resolved_path, label))
            continue

        if not stat.S_ISDIR(stat_result.st_mode):
            continue
        directory_identity = (stat_result.st_dev, stat_result.st_ino)
        if directory_identity in visited_directories:
            continue
        visited_directories.add(directory_identity)
        try:
            with os.scandir(path) as entries:
                pending.extend((Path(entry.path), label) for entry in entries)
        except OSError as exc:
            raise RuntimeError(f"seccomp kernel networking could not traverse startup path {path}: {exc}") from exc


def validate_seccomp_kernel_environment(kernel_manager: "AsyncKernelManager") -> SeccompKernelEnvironment:
    """Require every startup path to have an immutable ownership chain."""
    if os.geteuid() == 0:
        raise RuntimeError("seccomp kernel networking requires Workbench to run as a non-root user")

    kernel_uid = os.geteuid()
    executable_path = _kernel_python_executable(kernel_manager)
    inspected_paths = _validate_seccomp_startup_paths(
        _seccomp_kernel_initial_paths(executable_path),
        kernel_uid=kernel_uid,
    )
    startup_paths, import_paths, startup_tree_paths = _seccomp_kernel_startup_paths(executable_path)
    inspected_paths = _validate_seccomp_startup_paths(
        startup_paths,
        kernel_uid=kernel_uid,
        inspected_paths=inspected_paths,
    )
    _validate_seccomp_startup_trees(
        startup_tree_paths,
        kernel_uid=kernel_uid,
        inspected_paths=inspected_paths,
    )
    return SeccompKernelEnvironment(executable=executable_path, import_paths=import_paths)


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


def _seccomp_kernel_bootstrap_code(import_paths: tuple[Path, ...], environment_root: Path) -> str:
    return textwrap.dedent(
        f"""
        import ctypes as __agora_ctypes
        __agora_libc = __agora_ctypes.CDLL(None, use_errno=True)
        if __agora_libc.prctl({_PR_SET_DUMPABLE}, 0, 0, 0, 0) != 0:
            __agora_error = __agora_ctypes.get_errno()
            raise OSError(__agora_error, "failed to disable kernel process dumpability")
        if __agora_libc.prctl({_PR_GET_DUMPABLE}, 0, 0, 0, 0) != 0:
            raise RuntimeError("seccomp kernel networking could not disable kernel process dumpability")

        import os as __agora_os, sys as __agora_sys
        __agora_sys.prefix = {str(environment_root)!r}
        __agora_sys.exec_prefix = {str(environment_root)!r}
        __agora_sys.path.extend({[str(path) for path in import_paths]!r})
        from ipykernel.iostream import IOPubThread as __AgoraIOPubThread
        from ipykernel.kernelapp import IPKernelApp as __AgoraIPKernelApp

        def __agora_noop(self, *_args, **_kwargs):
            return None

        __agora_iopub_init = __AgoraIOPubThread.__init__
        __agora_iopub_send = __AgoraIOPubThread._really_send

        def __agora_init_iopub(self, socket, pipe=False, **kwargs):
            __agora_iopub_init(self, socket, pipe=False, **kwargs)

        def __agora_send_iopub(self, *args, **kwargs):
            if __agora_os.getpid() != self._master_pid:
                return None
            return __agora_iopub_send(self, *args, **kwargs)

        __AgoraIOPubThread.__init__ = __agora_init_iopub
        __AgoraIOPubThread._really_send = __agora_send_iopub
        __AgoraIPKernelApp.load_config_file = __agora_noop
        __AgoraIPKernelApp.init_path = __agora_noop
        __AgoraIPKernelApp.init_extensions = __agora_noop
        __AgoraIPKernelApp._run_startup_files = __agora_noop
        __agora_app = __AgoraIPKernelApp.instance()
        __agora_app.default_extensions = []
        __agora_app.extensions = []
        __agora_app.extra_extensions = []
        __agora_app.exec_PYTHONSTARTUP = False
        __agora_app.exec_files = []
        __agora_app.exec_lines = []
        __agora_app.file_to_run = ""
        __agora_app.code_to_run = ""
        __agora_app.module_to_run = ""
        __agora_app.initialize()
        __agora_app.start()
        """
    ).strip()


def configure_seccomp_kernel(
    kernel_manager: "AsyncKernelManager",
    environment: SeccompKernelEnvironment,
) -> Path:
    """Use IPC for Jupyter channels before installing a process-local filter."""
    ipc_dir, _ = _configure_ipc_kernel(kernel_manager)
    try:
        kernel_manager.kernel_spec.argv = [
            str(environment.executable),
            "-I",
            "-S",
            "-c",
            _seccomp_kernel_bootstrap_code(environment.import_paths, environment.executable.parent.parent),
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
        f"{_SECCOMP_INSTALL_CODE}\n"
        "import os as __agora_os, sys as __agora_sys\n"
        f"__agora_os.chdir({working_dir!r})\n"
        f"__agora_sys.path.insert(0, {working_dir!r})\n"
        "del __agora_os, __agora_sys\n"
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

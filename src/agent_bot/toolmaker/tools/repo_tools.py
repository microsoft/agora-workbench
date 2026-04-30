"""
FunctionTools for exploring a GitHub repository.

These tools give the ToolMaker agent read-only access to a cloned repo
so it can understand the codebase before generating a domain server.
"""

import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from agent_framework import FunctionTool
from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

# Workspace root where repos are cloned
_TOOLMAKER_WORKSPACE = Path(tempfile.gettempdir()) / "toolmaker_workspace"


def _get_repo_dir(repo_name: str) -> Path:
    """Get the directory for a cloned repo."""
    _sanitize_repo_name(repo_name)
    return _TOOLMAKER_WORKSPACE / repo_name


def _ensure_workspace() -> None:
    _TOOLMAKER_WORKSPACE.mkdir(parents=True, exist_ok=True)


def _validate_url(url: str) -> None:
    """Validate a URL to prevent SSRF attacks.

    Only http/https schemes are allowed.  The resolved IP address must not
    be loopback, private, link-local, or otherwise reserved.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}. Only http/https are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname.")

    try:
        infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname {hostname!r}: {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"URL resolves to blocked address {ip} (private/loopback/link-local/reserved).")


def _sanitize_repo_name(repo_name: str) -> str:
    """Validate repo_name to prevent path traversal."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo_name):
        raise ValueError(
            f"Invalid repo name: {repo_name!r}. Only alphanumerics, dots, hyphens, and underscores are allowed."
        )
    return repo_name


def _safe_path(repo_name: str, relative_path: str) -> Path:
    """Resolve a path safely, preventing traversal outside the repo directory."""
    repo_name = _sanitize_repo_name(repo_name)
    repo_dir = _get_repo_dir(repo_name).resolve()
    target = (repo_dir / relative_path).resolve()
    try:
        target.relative_to(repo_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: {relative_path}")
    return target


# ── Pydantic input models ────────────────────────────────────────────────


class CloneRepoInput(BaseModel):
    url: str = Field(description="GitHub repository URL (https://github.com/owner/repo)")
    branch: str = Field(default="main", description="Branch, tag, or commit to checkout")
    repo_name: str = Field(description="Short name for the repo (used as directory name)")


class ReadRepoFileInput(BaseModel):
    repo_name: str = Field(description="Name of the cloned repository")
    file_path: str = Field(description="Path relative to the repo root")


class ListRepoDirInput(BaseModel):
    repo_name: str = Field(description="Name of the cloned repository")
    dir_path: str = Field(default=".", description="Directory path relative to repo root")
    recursive: bool = Field(default=False, description="Whether to list recursively (max 3 levels)")


class SearchRepoInput(BaseModel):
    repo_name: str = Field(description="Name of the cloned repository")
    pattern: str = Field(description="Search pattern (grep-compatible regex)")
    file_glob: str = Field(default="*", description="File glob pattern to restrict search (e.g. '*.py')")


class BrowseUrlInput(BaseModel):
    url: str = Field(description="URL to fetch (documentation page, README, etc.)")


class RunBashInRepoInput(BaseModel):
    repo_name: str = Field(description="Name of the cloned repository")
    command: str = Field(description="Bash command to execute in the repo directory")
    timeout: int = Field(default=120, description="Timeout in seconds")


# ── Tool implementations ─────────────────────────────────────────────────


def create_repo_tools() -> list[FunctionTool]:
    """Create FunctionTools for repository exploration."""

    async def clone_repo(url: str, branch: str = "main", repo_name: str = "") -> str:
        """Clone a GitHub repository for exploration."""
        _ensure_workspace()
        if not repo_name:
            repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")

        repo_dir = _get_repo_dir(repo_name)
        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "50", "--branch", branch, url, str(repo_dir)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                # Try without --branch (might be a commit hash)
                result = subprocess.run(
                    ["git", "clone", "--depth", "50", url, str(repo_dir)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    return f"Error cloning repository: {result.stderr}"
                # Checkout specific commit/tag
                if branch != "main":
                    checkout = subprocess.run(
                        ["git", "checkout", branch],
                        capture_output=True,
                        text=True,
                        cwd=str(repo_dir),
                        timeout=30,
                    )
                    if checkout.returncode != 0:
                        return f"Cloned but failed to checkout '{branch}': {checkout.stderr}"

            # Get basic info
            file_count = sum(1 for _ in repo_dir.rglob("*") if _.is_file() and ".git" not in _.parts)
            readme_content = ""
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = repo_dir / readme_name
                if readme_path.exists():
                    readme_content = readme_path.read_text(errors="replace")[:8000]
                    break

            # Top-level listing
            top_level = []
            for item in sorted(repo_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                suffix = "/" if item.is_dir() else ""
                top_level.append(f"  {item.name}{suffix}")

            response = f"Successfully cloned '{repo_name}' ({file_count} files)\n"
            response += "Top-level contents:\n" + "\n".join(top_level)
            if readme_content:
                response += f"\n\n═══ README ═══\n{readme_content}"
            return response

        except subprocess.TimeoutExpired:
            return "Error: git clone timed out after 300 seconds"
        except Exception as e:
            return f"Error cloning repository: {e}"

    async def read_repo_file(repo_name: str, file_path: str) -> str:
        """Read a file from the cloned repository."""
        try:
            target = _safe_path(repo_name, file_path)
            if not target.exists():
                return f"Error: file not found: {file_path}"
            if not target.is_file():
                return f"Error: not a file: {file_path}"
            content = target.read_text(errors="replace")
            if len(content) > 50000:
                content = content[:50000] + f"\n\n... (truncated, {len(content)} total chars)"
            return content
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"

    async def list_repo_dir(repo_name: str, dir_path: str = ".", recursive: bool = False) -> str:
        """List directory contents in the cloned repository."""
        try:
            target = _safe_path(repo_name, dir_path)
            if not target.exists():
                return f"Error: directory not found: {dir_path}"
            if not target.is_dir():
                return f"Error: not a directory: {dir_path}"

            lines = []
            if recursive:
                for item in sorted(target.rglob("*")):
                    rel = item.relative_to(target)
                    # Skip hidden dirs and limit depth
                    if any(p.startswith(".") for p in rel.parts):
                        continue
                    if len(rel.parts) > 3:
                        continue
                    suffix = "/" if item.is_dir() else ""
                    lines.append(f"  {rel}{suffix}")
            else:
                for item in sorted(target.iterdir()):
                    if item.name.startswith("."):
                        continue
                    suffix = "/" if item.is_dir() else ""
                    size_info = ""
                    if item.is_file():
                        size = item.stat().st_size
                        size_info = f"  ({size} bytes)"
                    lines.append(f"  {item.name}{suffix}{size_info}")

            if not lines:
                return f"Directory '{dir_path}' is empty."
            return "\n".join(lines)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"

    async def search_repo(repo_name: str, pattern: str, file_glob: str = "*") -> str:
        """Search for a pattern across files in the repository using grep."""
        try:
            repo_dir = _get_repo_dir(repo_name)
            if not repo_dir.exists():
                return f"Error: repository '{repo_name}' not found. Clone it first."

            cmd = [
                "grep",
                "-rn",
                "--include",
                file_glob,
                "-E",
                pattern,
                str(repo_dir),
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 1:
                return f"No matches found for pattern: {pattern}"
            if result.returncode != 0:
                return f"Search error: {result.stderr}"

            # Make paths relative and truncate
            output_lines = []
            for line in result.stdout.split("\n")[:100]:
                line = line.replace(str(repo_dir) + "/", "")
                output_lines.append(line)

            output = "\n".join(output_lines)
            if len(result.stdout.split("\n")) > 100:
                output += f"\n... ({len(result.stdout.split(chr(10)))} total matches, showing first 100)"
            return output
        except subprocess.TimeoutExpired:
            return "Error: search timed out after 30 seconds"
        except Exception as e:
            return f"Error searching: {e}"

    async def browse_url(url: str) -> str:
        """Fetch a web page and return its text content (for documentation, READMEs, etc.)."""
        try:
            _validate_url(url)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            import urllib.request
            from html.parser import HTMLParser

            class TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.text_parts: list[str] = []
                    self._skip = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = True

                def handle_endtag(self, tag):
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = False

                def handle_data(self, data):
                    if not self._skip:
                        text = data.strip()
                        if text:
                            self.text_parts.append(text)

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="replace")

            extractor = TextExtractor()
            extractor.feed(content)
            text = "\n".join(extractor.text_parts)

            if len(text) > 15000:
                text = text[:15000] + "\n... (truncated)"
            return text if text.strip() else "(Page returned no text content)"
        except Exception as e:
            return f"Error fetching URL: {e}"

    # Commands allowed for exploration — read-only inspection of the repo.
    _ALLOWED_COMMANDS = frozenset(
        {
            "cat",
            "diff",
            "du",
            "echo",
            "env",
            "file",
            "find",
            "git",
            "grep",
            "head",
            "ls",
            "pip",
            "python",
            "python3",
            "sed",
            "sort",
            "tail",
            "tree",
            "uv",
            "wc",
            "which",
            "xargs",
        }
    )

    async def run_bash_in_repo(repo_name: str, command: str, timeout: int = 120) -> str:
        """Run a command in the context of the cloned repository.

        Only a curated set of read-only commands is allowed (ls, cat, git,
        grep, python, pip, etc.).  Shell operators like pipes and redirects
        are not supported — use separate calls instead.
        """
        import shlex

        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"Error: could not parse command: {exc}"

        if not argv:
            return "Error: empty command"

        base_cmd = os.path.basename(argv[0])
        if base_cmd not in _ALLOWED_COMMANDS:
            return (
                f"Error: command {base_cmd!r} is not in the allowlist. "
                f"Allowed commands: {', '.join(sorted(_ALLOWED_COMMANDS))}"
            )

        try:
            repo_dir = _get_repo_dir(repo_name)
            if not repo_dir.exists():
                return f"Error: repository '{repo_name}' not found. Clone it first."

            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=timeout,
                env={**os.environ, "PYTHONPATH": str(repo_dir)},
            )

            output = ""
            if result.stdout:
                output += f"stdout:\n{result.stdout[:10000]}"
            if result.stderr:
                output += f"\nstderr:\n{result.stderr[:5000]}"
            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"
            return output if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as e:
            return f"Error running command: {e}"

    return [
        FunctionTool(
            name="clone_repo",
            description=(
                "Clone a GitHub repository for exploration. Returns the top-level file listing and README content."
            ),
            func=clone_repo,
            input_model=CloneRepoInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="read_repo_file",
            description="Read a file from the cloned repository.",
            func=read_repo_file,
            input_model=ReadRepoFileInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="list_repo_dir",
            description="List directory contents in the cloned repository.",
            func=list_repo_dir,
            input_model=ListRepoDirInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="search_repo",
            description=(
                "Search for a regex pattern across files in the repository using grep. "
                "Use file_glob to restrict to specific file types (e.g. '*.py')."
            ),
            func=search_repo,
            input_model=SearchRepoInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="browse_url",
            description="Fetch a web page and return its text content (for docs, READMEs, etc.).",
            func=browse_url,
            input_model=BrowseUrlInput,
            approval_mode="never_require",
        ),
        FunctionTool(
            name="run_bash_in_repo",
            description=(
                "Run a bash command in the cloned repository directory. "
                "Use for non-destructive exploration: checking installed packages, "
                "running --help, listing CLI commands, inspecting config files, etc."
            ),
            func=run_bash_in_repo,
            input_model=RunBashInRepoInput,
            approval_mode="never_require",
        ),
    ]

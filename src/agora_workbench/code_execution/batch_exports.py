"""Bounded, server-owned snapshots for temporary parallel batch workers."""

import json
import logging
import os
import stat
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .object_transfer import MAX_TRANSFER_SIZE_BYTES, build_kernel_object_load_code

LOGGER = logging.getLogger(__name__)
MAX_BATCH_EXPORT_BYTES = MAX_TRANSFER_SIZE_BYTES
MAX_STAGED_EXPORT_BYTES = 4 * MAX_TRANSFER_SIZE_BYTES
MAX_STAGED_EXPORTS = 256
EXPORT_RETENTION_SECONDS = 15 * 60
RECEIPT_RETENTION_SECONDS = 5 * 60
MAX_COLLECTION_RECEIPTS = 128
MAX_PENDING_EXPORT_BATCHES = 128
RESULT_MARKER = "__AGORA_BATCH_EXPORT__"


class ExportStage:
    """Pin one private payload until it is collected or discarded."""

    def __init__(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="_agora_batch_export_"))
        directory_stat = self.directory.lstat()
        self.directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
        self.path = self.directory / "payload.pkl"
        self.fd = -1
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            file_stat = os.fstat(self.fd)
            self.identity = (file_stat.st_dev, file_stat.st_ino)
        except BaseException:
            if self.fd >= 0:
                os.close(self.fd)
                self.fd = -1
                self.path.unlink(missing_ok=True)
            self.directory.rmdir()
            raise

    def size(self) -> int:
        entry = self.path.lstat()
        if not stat.S_ISREG(entry.st_mode) or (entry.st_dev, entry.st_ino) != self.identity:
            raise RuntimeError("Batch export staging file identity changed.")
        return os.fstat(self.fd).st_size

    def close(self) -> None:
        if self.fd < 0:
            return
        try:
            try:
                entry = self.path.lstat()
                if (entry.st_dev, entry.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            os.close(self.fd)
            self.fd = -1
            try:
                entry = self.directory.lstat()
                if (entry.st_dev, entry.st_ino) == self.directory_identity:
                    self.directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Could not remove batch export staging directory.", exc_info=True)


def parse_export_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            payload = json.loads(line[len(RESULT_MARKER) :])
            if isinstance(payload, dict) and isinstance(payload.get("status"), str):
                return payload
    raise ValueError("Kernel did not return a batch export result.")


def build_export_code(variable: str, stage: ExportStage, max_bytes: int) -> str:
    function_name = f"__agora_export_{uuid.uuid4().hex}"
    return f"""
def {function_name}():
    import dill, json, os, stat
    namespace = globals()
    if {variable!r} not in namespace:
        print({RESULT_MARKER!r} + json.dumps({{"status": "missing"}}))
        return
    class LimitedWriter:
        def __init__(self, stream):
            self.stream = stream
            self.size = 0
        def write(self, data):
            if self.size + len(data) > {max_bytes}:
                raise OverflowError("Batch export size limit exceeded.")
            written = self.stream.write(data)
            self.size += written
            return written
    descriptor = os.open({str(stage.path)!r}, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != {stage.identity!r}:
            raise RuntimeError("Batch export staging file identity changed.")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            writer = LimitedWriter(stream)
            try:
                dill.dump(namespace[{variable!r}], writer)
            except OverflowError:
                outcome = {{"status": "too_large"}}
            except Exception:
                outcome = {{"status": "failed", "error": "Object serialization failed. Export portable data instead."}}
            else:
                outcome = {{"status": "staged", "bytes": writer.size}}
    finally:
        os.close(descriptor)
    print({RESULT_MARKER!r} + json.dumps(outcome))
try:
    {function_name}()
finally:
    del {function_name}
"""


def build_collection_code(stages: dict[int, ExportStage], import_as: str, overwrite: bool) -> str:
    function_name = f"__agora_collect_{uuid.uuid4().hex}"
    loads = ""
    for index, stage in stages.items():
        loads += f"index = {index}\n"
        loads += build_kernel_object_load_code("__agora_value__", str(stage.path), stage.identity)
        loads += f"values[{index}] = __agora_value__\n"
    return f"""
def {function_name}():
    import json
    namespace = globals()
    if {not overwrite!r} and {import_as!r} in namespace:
        print({RESULT_MARKER!r} + json.dumps({{"status": "destination_exists"}}))
        return
    values = {{}}
    index = None
    try:
{textwrap.indent(loads, "        ")}
    except Exception:
        print({RESULT_MARKER!r} + json.dumps({{"status": "load_failed", "input_index": index}}))
        return
    namespace[{import_as!r}] = values
    print({RESULT_MARKER!r} + json.dumps({{"status": "collected"}}))
try:
    {function_name}()
finally:
    del {function_name}
"""

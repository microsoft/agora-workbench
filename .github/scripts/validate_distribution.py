import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def _metadata_value(content: bytes, field: str) -> str:
    value = BytesParser().parsebytes(content)[field]
    if value is None:
        raise SystemExit(f"Distribution metadata is missing {field}")
    return value


project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
expected_name = project["name"]
expected_version = project["version"]

distributions = sorted(Path("dist").iterdir())
wheels = [path for path in distributions if path.suffix == ".whl"]
sdists = [path for path in distributions if path.name.endswith(".tar.gz")]
if len(wheels) != 1 or len(sdists) != 1:
    raise SystemExit(f"Expected one wheel and one sdist, found: {distributions}")

with zipfile.ZipFile(wheels[0]) as archive:
    wheel_files = archive.namelist()
    metadata_files = [name for name in wheel_files if name.endswith(".dist-info/METADATA")]
    if len(metadata_files) != 1:
        raise SystemExit(f"Expected one wheel METADATA file, found: {metadata_files}")
    wheel_metadata = archive.read(metadata_files[0])

with tarfile.open(sdists[0]) as archive:
    sdist_files = archive.getnames()
    metadata_members = [
        member for member in archive.getmembers() if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
    ]
    if len(metadata_members) != 1:
        raise SystemExit(f"Expected one sdist PKG-INFO file, found: {[member.name for member in metadata_members]}")
    metadata_file = archive.extractfile(metadata_members[0])
    if metadata_file is None:
        raise SystemExit("Could not read sdist PKG-INFO")
    sdist_metadata = metadata_file.read()

for label, metadata in (("wheel", wheel_metadata), ("sdist", sdist_metadata)):
    name = _metadata_value(metadata, "Name")
    version = _metadata_value(metadata, "Version")
    if name != expected_name or version != expected_version:
        raise SystemExit(
            f"{label} metadata does not match pyproject.toml: "
            f"expected {expected_name} {expected_version}, found {name} {version}"
        )

forbidden = [
    name
    for name in wheel_files + sdist_files
    if "/tests/" in f"/{name}/"
    or "/__pycache__/" in f"/{name}/"
    or name.endswith((".pyc", ".pyo"))
]
if forbidden:
    raise SystemExit("Distribution contains excluded files:\n" + "\n".join(forbidden))

required_wheel_files = {
    "agora_workbench/__init__.py",
    "agora_workbench/deployment/templates/docker/Dockerfile",
    "agora_workbench/deployment/templates/azure/main.bicep",
}
missing = sorted(required_wheel_files.difference(wheel_files))
if missing:
    raise SystemExit("Wheel is missing required files:\n" + "\n".join(missing))

print(f"Validated {wheels[0].name} and {sdists[0].name}")

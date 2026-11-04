# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Allowlisted source zip. No credentials, developer state, tests or infrastructure enter it."""

import hashlib
import json
import os
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_FILES = (
    "__init__.py", "main.py", "config.py", "access.py", "capacity.py", "bridge.py",
    "agent.py", "activity.py", "webiq.py", "protocol.py", "performance.py", "login.html",
    "agent_validation.py", "credentials.py",
)


def locked_requirements(root: Path) -> str:
    manifest = tomllib.loads((root / "pyproject.toml").read_text())
    pending = [Requirement(value) for value in manifest["project"]["dependencies"]]
    versions: dict[str, str] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    environment = {**default_environment(), "sys_platform": "linux", "platform_system": "Linux",
                   "platform_machine": "x86_64", "python_version": "3.14", "python_full_version": "3.14.5"}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        key = (name, frozenset(requirement.extras))
        installed = distribution(name)
        if not requirement.specifier.contains(installed.version):
            raise ValueError(f"Installed dependency {name} does not satisfy the manifest.")
        if key in seen:
            continue
        seen.add(key)
        versions[name] = installed.version
        for value in installed.requires or []:
            child = Requirement(value)
            if child.marker is None or any(child.marker.evaluate({**environment, "extra": extra})
                                           for extra in requirement.extras | {""}):
                pending.append(child)
    return "".join(f"{name}=={versions[name]}\n" for name in sorted(versions))


def package(root: Path, destination: Path) -> dict:
    if any((root / directory).is_symlink() for directory in ("app", "dist", "dist/assets")):
        raise ValueError("Package directories must not be symbolic links.")
    files = [root / "app" / name for name in RUNTIME_FILES]
    files += [root / "dist" / "index.html", root / "dist" / "audio-worklet.js"]
    files += sorted((root / "dist" / "assets").glob("*.js"))
    files += sorted((root / "dist" / "assets").glob("*.css"))
    if not any(file.suffix == ".js" and file.parent.name == "assets" for file in files):
        raise ValueError("Build the frontend before packaging.")
    for path in files:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Missing or unsafe allowlisted package member.")
    requirements = locked_requirements(root)
    # Exclusive creation prevents a retry from overwriting the reviewed rollback artifact.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output, ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr("requirements.txt", requirements)
    with ZipFile(destination) as archive:
        names = archive.namelist()
        if archive.testzip() is not None or set(names) != {
            *(path.relative_to(root).as_posix() for path in files), "requirements.txt",
        }:
            raise ValueError("Package integrity check failed.")
    return {"file": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "members": names, "lockedDependencies": len(requirements.splitlines())}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m infra.package_app ABSOLUTE_PATH_TO_NEW_ZIP")
    target = Path(sys.argv[1])
    if not target.is_absolute() or not target.parent.resolve().is_relative_to(ROOT / ".deploy"):
        raise SystemExit("Package destination must be a new file in an existing .deploy directory.")
    print(json.dumps(package(ROOT, target), indent=2))

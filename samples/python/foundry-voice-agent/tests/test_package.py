# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path
from zipfile import ZipFile

import pytest

from infra import package_app


@pytest.fixture
def package_root(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    for name in package_app.RUNTIME_FILES:
        (tmp_path / "app" / name).write_text("test runtime fixture")
    for name in ("index.html", "audio-worklet.js", "assets/index-a.js", "assets/index-b.css"):
        (tmp_path / "dist" / name).write_text("test asset fixture")
    monkeypatch.setattr(package_app, "locked_requirements", lambda root: "test-runtime==1.0\n")
    return tmp_path


def test_package_is_allowlisted_and_private(package_root):
    root = package_root
    for name in (".env", ".private-demo/access.json", ".foundry/agent.json",
                 ".logs/performance.jsonl", "app/setup_agent.py", "dist/assets/source.js.map"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TEST_SECRET_MUST_NOT_ENTER_PACKAGE")
    target = root / "demo.zip"
    result = package_app.package(root, target)
    assert target.stat().st_mode & 0o777 == 0o600
    assert result["lockedDependencies"] == 1
    with ZipFile(target) as archive:
        assert len(archive.namelist()) == len(package_app.RUNTIME_FILES) + 5
        assert not any(b"TEST_SECRET" in archive.read(name) for name in archive.namelist())
        assert archive.read("requirements.txt") == b"test-runtime==1.0\n"
    with pytest.raises(FileExistsError):
        package_app.package(root, target)


def test_package_rejects_missing_build(package_root):
    (package_root / "dist" / "assets" / "index-a.js").unlink()
    with pytest.raises(ValueError, match="Build the frontend"):
        package_app.package(package_root, package_root / "demo.zip")
    assert not (package_root / "demo.zip").exists()


def test_package_rejects_symlinked_member(package_root):
    member = package_root / "app" / "main.py"
    member.unlink()
    member.symlink_to(package_root / "app" / "config.py")
    with pytest.raises(ValueError, match="unsafe"):
        package_app.package(package_root, package_root / "demo.zip")


def test_package_rejects_symlinked_asset_directory(package_root):
    assets = package_root / "dist" / "assets"
    renamed = package_root / "original-assets"
    assets.rename(renamed)
    assets.symlink_to(renamed)
    with pytest.raises(ValueError, match="directories"):
        package_app.package(package_root, package_root / "demo.zip")


def test_runtime_lock_is_pinned_and_includes_voice_transport():
    lines = package_app.locked_requirements(Path(__file__).resolve().parent.parent).splitlines()
    assert "azure-ai-projects==2.7.0" in lines
    assert any(line.startswith("aiohttp==") for line in lines)
    assert all(line.count("==") == 1 and "://" not in line for line in lines)
    assert not any(line.startswith(("pytest==", "ruff==", "webiq-foundry-voice-agent==")) for line in lines)

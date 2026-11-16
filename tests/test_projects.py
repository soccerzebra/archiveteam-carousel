"""Installer tests for docker-image projects — crane/tar are faked, so no registry or daemon."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import controlplane.projects as projects_mod
from controlplane.models import ProjectInfo, ProjectSource
from controlplane.projects import ProjectInstaller

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def fake_crane(digest: str):
    """A fake _run that simulates `crane digest`, `crane export`, and `tar -x` of /grab."""

    async def _run(*args, cwd=None):
        if args[:2] == ("crane", "digest"):
            return digest
        if args[:2] == ("crane", "export"):
            pathlib.Path(args[3]).write_bytes(b"fake-tar")  # the export tarball
            return ""
        if args[0] == "tar":  # tar -x -C <dest> --strip-components=1 -f <tar> grab
            dest = pathlib.Path(args[args.index("-C") + 1])
            (dest / "pipeline.py").write_text("project = 1\npipeline = 1\n")
            (dest / "warrior-install.sh").write_text("#!/bin/bash\nexit 0\n")
            return ""
        return ""

    return _run


def make_installer(tmp_path):
    return ProjectInstaller(
        projects_dir=pathlib.Path(tmp_path) / "projects",
        versions_dir=pathlib.Path(tmp_path) / "versions",
    )


DOCKER_PROJECT = ProjectInfo(
    name="instagram", title="Instagram", source=ProjectSource.DOCKER, docker="atdr/instagram-grab"
)


async def test_image_tag_parses_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects_mod, "_run", fake_crane(DIGEST_A))
    inst = make_installer(tmp_path)
    assert await inst._image_tag("atdr/instagram-grab") == "a" * 12


async def test_docker_install_extracts_grab_and_pins_by_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects_mod, "_run", fake_crane(DIGEST_A))
    inst = make_installer(tmp_path)
    version_dir = await inst.prepare(DOCKER_PROJECT)
    # extracted /grab landed as a plain checkout, pinned by digest prefix
    assert version_dir.name == "instagram-" + "a" * 12
    assert (version_dir / "pipeline.py").exists()
    assert (version_dir / "warrior-install.sh").exists()
    # the transient export tar is cleaned up
    assert not any(p.name.endswith(".export.tar") for p in (inst.projects_dir).iterdir())


async def test_docker_has_update_compares_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects_mod, "_run", fake_crane(DIGEST_A))
    inst = make_installer(tmp_path)
    version_dir = await inst.prepare(DOCKER_PROJECT)
    # same digest => up to date
    assert await inst.has_update(DOCKER_PROJECT, version_dir) is False
    # registry now serves a different digest => update available
    monkeypatch.setattr(projects_mod, "_run", fake_crane(DIGEST_B))
    assert await inst.has_update(DOCKER_PROJECT, version_dir) is True

"""Capability artifacts on disk, as YAML.

Reading YAML is this adapter's mechanism. It hands back the document and stops
there: parsing it into a Capability, and deciding whether it describes one, is
the domain's job. A store that did both would become the authority on what a
valid capability is, which is not something a filesystem should decide.

One file per version, named for both:

    capabilities/lookup_member_balance.v1.0.0.yaml

Versions are never overwritten. An artifact that was approved has to stay
byte-identical to the thing that was approved, so saving over one is refused
rather than resolved in favour of whoever wrote last.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cua.adapters.errors import ConfigurationError
from cua.domain.errors import ArtifactNotFound

SUFFIX = ".yaml"
VERSION_MARKER = ".v"

# Loose on purpose. Ordering needs the numeric parts; anything else sorts as
# text after them, so a prerelease lands before the release it precedes.
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


@dataclass(frozen=True)
class FilesystemArtifactStore:
    """Capability documents in one directory."""

    root: Path

    def save(self, name: str, version: str, document: Mapping[str, Any]) -> str:
        """Write a version, or refuse because it is already there."""
        path = self._path(name, version)
        if path.exists():
            raise ConfigurationError(
                f"{path} already exists; versions are not overwritten, publish a new one"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(dict(document), sort_keys=False), encoding="utf-8")
        return str(path)

    def load(self, name: str, version: str) -> Mapping[str, Any]:
        """Read a version back, or say which one is missing."""
        path = self._path(name, version)
        if not path.exists():
            raise ArtifactNotFound(name, version)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ArtifactNotFound(name, version)
        return document

    def list_versions(self, name: str) -> Sequence[str]:
        """Known versions, oldest first."""
        prefix = f"{name}{VERSION_MARKER}"
        versions = [
            path.name[len(prefix) : -len(SUFFIX)]
            for path in self.root.glob(f"{prefix}*{SUFFIX}")
        ]
        return sorted(versions, key=_version_order)

    def _path(self, name: str, version: str) -> Path:
        return self.root / f"{name}{VERSION_MARKER}{version}{SUFFIX}"


def _version_order(version: str) -> tuple[int, int, int, str]:
    """Sort semantically rather than as text, so 10.0.0 follows 9.0.0."""
    match = SEMVER.match(version)
    if match is None:
        return (0, 0, 0, version)
    major, minor, patch, rest = match.groups()
    return (int(major), int(minor), int(patch), rest)

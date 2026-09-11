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
from cua.domain.errors import ArtifactNotFound, MalformedArtifact

SUFFIX = ".yaml"
VERSION_MARKER = ".v"

# A name and a version become a filename, so they are identifiers and nothing
# else. Without this a capability called "../escaped" writes outside the store.
SAFE_NAME = re.compile(r"^[a-z0-9_]+$")
SAFE_VERSION = re.compile(r"^[0-9A-Za-z.\-+]+$")

# Loose on purpose. Ordering needs the numeric parts; anything else sorts as
# text after them, so a prerelease lands before the release it precedes.
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


@dataclass(frozen=True)
class FilesystemArtifactStore:
    """Capability documents in one directory."""

    root: Path

    def save(self, name: str, version: str, document: Mapping[str, Any]) -> str:
        """Write a version, or refuse because it is already there.

        Created exclusively rather than checked and then written. Asking
        whether a file exists and then writing it leaves a gap in which
        somebody else can publish the same version, and the whole point of
        refusing is that an approved artifact stays what it was.
        """
        path = self._path(name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(yaml.safe_dump(dict(document), sort_keys=False))
        except FileExistsError as error:
            raise ConfigurationError(
                f"{path} already exists; versions are not overwritten, publish a new one"
            ) from error
        except OSError as error:
            raise ConfigurationError(f"could not write {path}: {error}") from error
        return str(path)

    def load(self, name: str, version: str) -> Mapping[str, Any]:
        """Read a version back, or say which one is missing."""
        path = self._path(name, version)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ArtifactNotFound(name, version) from error
        except OSError as error:
            raise ConfigurationError(f"could not read {path}: {error}") from error

        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            # A vendor exception stops here. Everything above this line handles
            # domain errors, and a parser's exception type is not one.
            raise MalformedArtifact(f"{path} is not valid YAML: {error}") from error

        if not isinstance(document, dict):
            raise ArtifactNotFound(name, version)
        return document

    def list_versions(self, name: str) -> Sequence[str]:
        """Known versions, oldest first."""
        _require_identifier(name, SAFE_NAME, "capability name")
        prefix = f"{name}{VERSION_MARKER}"
        versions = [
            path.name[len(prefix) : -len(SUFFIX)]
            for path in self.root.glob(f"{prefix}*{SUFFIX}")
        ]
        return sorted(versions, key=_version_order)

    def _path(self, name: str, version: str) -> Path:
        """Where this version lives, or a refusal if the name is not a name.

        Both halves become a filename. A separator or a traversal segment in
        either would put the file somewhere else entirely, so they are checked
        before they are joined rather than after.
        """
        _require_identifier(name, SAFE_NAME, "capability name")
        _require_identifier(version, SAFE_VERSION, "capability version")
        return self.root / f"{name}{VERSION_MARKER}{version}{SUFFIX}"


def _require_identifier(value: str, pattern: re.Pattern[str], label: str) -> None:
    if ".." in value or not pattern.match(value):
        raise ConfigurationError(f"invalid {label} {value!r}")


def _version_order(version: str) -> tuple[int, int, int, str]:
    """Sort semantically rather than as text, so 10.0.0 follows 9.0.0."""
    match = SEMVER.match(version)
    if match is None:
        return (0, 0, 0, version)
    major, minor, patch, rest = match.groups()
    return (int(major), int(minor), int(patch), rest)

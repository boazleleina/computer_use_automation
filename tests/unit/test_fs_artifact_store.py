"""Capability artifacts on disk.

The store hands back a document and nothing more. Whether that document
describes a capability is decided by the domain, so the two are tested apart:
here, that the right bytes come back; elsewhere, that they mean something.
"""

from pathlib import Path

import pytest
import yaml

from cua.adapters.errors import ConfigurationError
from cua.adapters.fs_artifact_store import FilesystemArtifactStore
from cua.domain.errors import ArtifactNotFound
from cua.ports.artifact_store import ArtifactStore

DOCUMENT = {"schema_version": "1.0", "contract": {"id": "lookup", "version": "1.0.0"}}


def wire(store: ArtifactStore) -> ArtifactStore:
    """Typed slot. mypy proves the store satisfies the port here."""
    return store


@pytest.fixture
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(root=tmp_path)


def test_a_saved_version_reads_back(store):
    store.save("lookup", "1.0.0", DOCUMENT)
    assert store.load("lookup", "1.0.0") == DOCUMENT


def test_the_file_is_named_for_the_capability_and_the_version(store, tmp_path):
    """Both in the name, so a directory of artifacts is readable without
    opening any of them."""
    path = store.save("lookup", "1.0.0", DOCUMENT)
    assert Path(path).name == "lookup.v1.0.0.yaml"


def test_a_version_is_never_overwritten(store):
    """An artifact that was approved has to stay byte-identical to the thing
    that was approved. Saving over it is refused rather than resolved in favour
    of whoever wrote last.
    """
    store.save("lookup", "1.0.0", DOCUMENT)

    with pytest.raises(ConfigurationError):
        store.save("lookup", "1.0.0", {"contract": {"id": "something else"}})

    assert store.load("lookup", "1.0.0") == DOCUMENT


def test_a_missing_version_says_which_one(store):
    """Raising rather than returning None, so a missing artifact cannot be
    mistaken for an empty one."""
    store.save("lookup", "1.0.0", DOCUMENT)

    with pytest.raises(ArtifactNotFound) as raised:
        store.load("lookup", "2.0.0")

    assert "2.0.0" in str(raised.value)


def test_versions_are_listed_semantically_not_alphabetically(store):
    """9.0.0 comes before 10.0.0. As text it does not."""
    for version in ("1.0.0", "10.0.0", "2.1.0", "9.0.0"):
        store.save("lookup", version, DOCUMENT)

    assert store.list_versions("lookup") == ["1.0.0", "2.1.0", "9.0.0", "10.0.0"]


def test_listing_a_capability_nobody_has_saved_is_empty_not_an_error(store):
    assert store.list_versions("nothing") == []


def test_versions_of_other_capabilities_are_not_listed(store):
    store.save("lookup", "1.0.0", DOCUMENT)
    store.save("open_subaccount", "3.0.0", DOCUMENT)

    assert store.list_versions("lookup") == ["1.0.0"]


def test_a_file_that_is_not_a_mapping_is_not_an_artifact(store, tmp_path):
    """Valid YAML and not a capability. It fails as missing rather than
    arriving as a string for something else to choke on."""
    (tmp_path / "lookup.v1.0.0.yaml").write_text("just a sentence\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFound):
        store.load("lookup", "1.0.0")


def test_the_store_satisfies_the_port(store):
    wire(store)


def test_the_handwritten_artifact_survives_a_round_trip(store):
    """Saved and read back, the document still parses into the same capability.

    This is the chain the CLI needs: a file on disk, a document, a Capability.
    """
    from cua.domain.artifact import capability_from_document

    with open("tests/fixtures/member_lookup.handwritten.yaml", encoding="utf-8") as handle:
        original = yaml.safe_load(handle)

    store.save("lookup_member_balance", "1.0.0", original)
    capability = capability_from_document(store.load("lookup_member_balance", "1.0.0"))

    assert capability.contract.name == "lookup_member_balance"
    assert [step.id for step in capability.steps] == [
        "enter_member_number",
        "submit_lookup",
        "read_savings_balance",
        "read_account_name",
    ]

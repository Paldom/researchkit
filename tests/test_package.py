"""Package-level metadata tests."""

from researchkit import __version__


def test_version_metadata_resolves() -> None:
    # No hardcoded number: a pin would break every release's publish gate.
    assert __version__

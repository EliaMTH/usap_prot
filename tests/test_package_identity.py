"""
Package-level identity and interchange-format guarantees.

These cover the facts a future W3C Web Annotation export has to build on and
that cannot be added afterwards without rewriting existing packages: a stable
package identity, a parseable content hash, and timestamps in a standard
lexical form. See FEATURE_W3C_WEB_ANNOTATION_PROFILE.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import make_pkg, write_tiny_mesh
from usap import USAPError, USAPPackage, register_mesh_asset
from usap._util import canonical_hash, mint_package_iri, parse_content_hash
from usap.constants import CURRENT_PROFILE_VERSION

# UTC ISO-8601 to the second, which is what the schema default emits.
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

UUID_URN = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Package identity
# ---------------------------------------------------------------------------


def test_package_iri_is_minted_and_stable(tmp_path: Path) -> None:
    # Identity has to be born with the package: an IRI invented at read time
    # would differ between two readers of the same file.
    db_path = tmp_path / "identity.usap.gpkg"

    with make_pkg(tmp_path, "identity.usap.gpkg") as pkg:
        minted = pkg.get_package_iri()

    assert UUID_URN.match(minted), minted

    with USAPPackage.open(db_path) as reopened:
        assert reopened.get_package_iri() == minted


def test_each_package_gets_its_own_iri(tmp_path: Path) -> None:
    with make_pkg(tmp_path, "a.usap.gpkg") as a:
        with make_pkg(tmp_path, "b.usap.gpkg") as b:
            assert a.get_package_iri() != b.get_package_iri()


def test_explicit_package_iri_is_honoured(tmp_path: Path) -> None:
    # Adopting an identity that already exists elsewhere must be possible;
    # only the default is minted.
    adopted = "https://example.org/packages/area-2026"

    with USAPPackage.create(
        tmp_path / "adopted.usap.gpkg",
        overwrite=True,
        package_iri=adopted,
    ) as pkg:
        assert pkg.get_package_iri() == adopted


def test_blank_package_iri_is_reported(tmp_path: Path) -> None:
    # The column is NOT NULL, so this is the shape a package written by
    # something other than USAPPackage.create could still take.
    with make_pkg(tmp_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute("UPDATE usap_profile SET package_iri = '  '")

        report = pkg.validate_report()

        assert "INVALID_PACKAGE_IRI" in [i.code for i in report.issues]
        assert not report.is_ok

        with pytest.raises(USAPError, match="no package_iri"):
            pkg.get_package_iri()


def test_profile_version_is_current(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        row = pkg.conn.execute(
            "SELECT profile_version FROM usap_profile"
        ).fetchone()

        assert row["profile_version"] == CURRENT_PROFILE_VERSION


def test_extension_definition_carries_the_profile_version(
    tmp_path: Path,
) -> None:
    # The version rides in the URI path, so it must follow the version being
    # written rather than whatever was current when the constant was edited.
    with make_pkg(tmp_path) as pkg:
        definitions = {
            row["definition"]
            for row in pkg.conn.execute(
                "SELECT definition FROM gpkg_extensions"
            )
        }

        assert definitions == {
            f"https://usap.invalid/extensions/usap_core/"
            f"{CURRENT_PROFILE_VERSION}"
        }


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def test_parse_content_hash_accepts_canonical_and_bare() -> None:
    digest = "a" * 64

    assert parse_content_hash(f"sha256:{digest}") == ("sha256", digest)

    # A bare digest predates the canonical form; reading it as sha-256 is what
    # keeps it comparable to a freshly computed one.
    assert parse_content_hash(digest) == ("sha256", digest)
    assert parse_content_hash(digest.upper()) == ("sha256", digest)


def test_parse_content_hash_rejects_non_digests() -> None:
    # content_hash is free text, so a caller token must come back as "no
    # comparable hash" rather than raise or be mistaken for a digest.
    for value in (None, "", "synthetic_buildings_10", "sha256:", "zz" * 32):
        assert parse_content_hash(value) is None, value


def test_adapters_write_canonical_hashes(tmp_path: Path) -> None:
    mesh_path = tmp_path / "tiny.ply"
    write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        register_mesh_asset(pkg, mesh_path, representation_name="tiny")

        row = pkg.conn.execute(
            "SELECT content_hash FROM usap_asset"
        ).fetchone()

        assert row["content_hash"] == canonical_hash(mesh_path)
        assert row["content_hash"].startswith("sha256:")


def test_verify_assets_matches_a_legacy_bare_digest(tmp_path: Path) -> None:
    # The whole point of tolerant parsing: a package written before the
    # canonical form must not report every asset as 'changed'.
    from usap.validation import verify_assets

    mesh_path = tmp_path / "tiny.ply"
    write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        register_mesh_asset(pkg, mesh_path, representation_name="tiny")

        bare = canonical_hash(mesh_path).split(":", 1)[1]

        with pkg.transaction():
            pkg.conn.execute(
                "UPDATE usap_asset SET content_hash = ?", (bare,)
            )

        results = verify_assets(pkg.conn)

        assert [r["status"] for r in results] == ["ok"]
        # Reported canonically whatever was stored.
        assert results[0]["actual_hash"].startswith("sha256:")


def test_verify_assets_detects_a_changed_file(tmp_path: Path) -> None:
    from usap.validation import verify_assets

    mesh_path = tmp_path / "tiny.ply"
    write_tiny_mesh(mesh_path)

    with make_pkg(tmp_path) as pkg:
        register_mesh_asset(pkg, mesh_path, representation_name="tiny")

        mesh_path.write_bytes(mesh_path.read_bytes() + b"\n# touched\n")

        assert [r["status"] for r in verify_assets(pkg.conn)] == ["changed"]


def test_non_canonical_hash_is_a_deep_warning(tmp_path: Path) -> None:
    # A warning, not an error: register_asset accepts any token, so this is
    # unusual rather than corrupt. What it costs is verifiability.
    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(
            uri="area.ply",
            asset_kind="mesh",
            content_hash="not-a-digest",
        )

        report = pkg.validate_report(level="deep")
        issues = [i for i in report.issues if i.code == "NON_CANONICAL_CONTENT_HASH"]

        assert len(issues) == 1
        assert issues[0].severity == "warning"
        # Warnings do not make a package invalid.
        assert report.is_ok


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_annotation_timestamps_are_utc_iso8601(tmp_path: Path) -> None:
    from conftest import make_mesh_part

    with make_pkg(tmp_path) as pkg:
        part_id = make_mesh_part(pkg)

        class_id = pkg.create_semantic_class(
            scheme="local",
            class_uri="local:Thing",
            local_name="Thing",
        )

        annotation_id = pkg.create_annotation(
            annotation_uid="ann_iso",
            semantic_class_id=class_id,
        )

        pkg.replace_annotation_membership(
            annotation_id=annotation_id,
            asset_part_id=part_id,
            element_kind="face",
            element_indices=[1, 2],
        )

        row = pkg.conn.execute(
            """
            SELECT created_at, updated_at
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert ISO_UTC.match(row["created_at"]), row["created_at"]
        assert ISO_UTC.match(row["updated_at"]), row["updated_at"]

        # update_annotation writes its own timestamp; it has to use the same
        # spelling as the schema default or edited rows drift into a second
        # format.
        pkg.update_annotation(annotation_id, status="accepted")

        updated = pkg.conn.execute(
            """
            SELECT updated_at
            FROM usap_annotation
            WHERE annotation_id = ?
            """,
            (annotation_id,),
        ).fetchone()

        assert ISO_UTC.match(updated["updated_at"]), updated["updated_at"]


def test_edit_log_timestamps_are_utc_iso8601(tmp_path: Path) -> None:
    with make_pkg(tmp_path) as pkg:
        pkg.create_semantic_class(
            scheme="local",
            class_uri="local:Thing",
            local_name="Thing",
        )

        row = pkg.conn.execute(
            "SELECT created_at FROM usap_edit_log LIMIT 1"
        ).fetchone()

        assert ISO_UTC.match(row["created_at"]), row["created_at"]


def test_mint_package_iri_is_unique() -> None:
    assert mint_package_iri() != mint_package_iri()

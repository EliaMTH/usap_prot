from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import make_pkg
from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPError,
    USAPPackage,
    create_synthetic_package,
    validate_connection,
)
from usap._util import sha256_file
from usap.validation import verify_assets


def _create_small_valid_package(db_path: Path):
    return create_synthetic_package(
        db_path,
        config=SyntheticConfig(
            building_count=5,
            roof_faces_per_building=20,
            wall_faces_per_building=30,
            ground_faces_per_building=10,
        ),
        overwrite=True,
    )


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def test_validation_report_is_ok_for_valid_synthetic_package(tmp_path: Path) -> None:
    db_path = tmp_path / "valid.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        report = pkg.validate_report()

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert report.issues == []


def test_validation_catches_corrupt_membership_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt_payload.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET payload = X'000102'
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "CORRUPT_MEMBERSHIP_PAYLOAD" in _codes(report)


def test_validation_catches_primary_object_without_represents_link(
    tmp_path: Path,
) -> None:
    # The primary city object is stored both as a column and as a 'represents'
    # link. A package where those disagree answers city-object queries with
    # annotations that no longer belong to the object, so it is not valid.
    db_path = tmp_path / "stale_link.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        annotation = pkg.conn.execute(
            """
            SELECT annotation_id, primary_city_object_id
            FROM usap_annotation
            WHERE primary_city_object_id IS NOT NULL
            LIMIT 1
            """
        ).fetchone()

        assert annotation is not None

        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_annotation_object
                WHERE annotation_id = ?
                  AND city_object_id = ?
                  AND relation_type = 'represents'
                """,
                (
                    annotation["annotation_id"],
                    annotation["primary_city_object_id"],
                ),
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "ANNOTATION_PRIMARY_OBJECT_LINK_MISSING" in _codes(report)


def test_validation_catches_membership_count_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_count.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET element_count = element_count + 1
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MEMBERSHIP_COUNT_MISMATCH" in _codes(report)


def test_validation_catches_containment_cycle(tmp_path: Path) -> None:
    # "An object and its parts" only means something if containment is
    # acyclic: on a cycle every object is its own part, so a package that
    # states one is stating something it cannot mean. Descendant queries do
    # not hang (the recursive CTE deduplicates), which is exactly why this
    # has to be reported rather than left to fail loudly at query time.
    db_path = tmp_path / "cycle.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        parent = pkg.resolve_city_object("building_000000")
        child = pkg.resolve_city_object("building_000000_roof")

        pkg.link_city_objects(
            child,
            parent,
            "contains",
            category="containment",
        )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "CITY_OBJECT_GRAPH_CYCLE" in _codes(report)


def test_non_containment_cycle_is_not_an_error(tmp_path: Path) -> None:
    # The graph is typed: two objects can perfectly well be adjacent to each
    # other. Only containment edges are checked for cycles.
    db_path = tmp_path / "adjacency_cycle.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        first = pkg.resolve_city_object("building_000000")
        second = pkg.resolve_city_object("building_000000_roof")

        for parent, child in [(first, second), (second, first)]:
            pkg.link_city_objects(
                parent,
                child,
                "adjacentTo",
                category="peer",
            )

        report = pkg.validate_report()

        assert "CITY_OBJECT_GRAPH_CYCLE" not in _codes(report)
        assert report.is_ok, [issue.format() for issue in report.issues]


def test_validation_catches_missing_semantic_class_closure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing_class_closure.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_semantic_class_closure
                WHERE ancestor_class_id = 1
                  AND descendant_class_id = 1
                  AND depth = 0
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MISSING_SEMANTIC_CLASS_SELF_CLOSURE" in _codes(report)


def test_validation_warns_on_duplicate_relationship_edges(pkg: USAPPackage) -> None:
    # link_city_objects dedups identical edges, but packages written before
    # that guard (or via raw SQL) may hold duplicates. Validation must
    # surface them — as a warning, not an error, because such packages must
    # keep opening and updating.
    parent = pkg.create_city_object(object_uid="b1")
    child = pkg.create_city_object(object_uid="b1_roof")

    type_id = pkg.register_relationship_type(
        "boundary",
        code_space="http://www.opengis.net/citygml/3.0",
        category="containment",
    )

    pkg.link_city_objects(parent, child, type_id, role="roof")

    # Simulate a duplicate written behind the API's back.
    with pkg.transaction():
        pkg.conn.execute(
            """
            INSERT INTO usap_city_object_relationship (
                graph_name, from_city_object_id, to_city_object_id,
                relationship_type_id, role
            )
            VALUES ('usap_default', ?, ?, ?, 'roof')
            """,
            (parent, child, type_id),
        )

    report = pkg.validate_report()
    duplicates = [
        issue for issue in report.issues
        if issue.code == "DUPLICATE_RELATIONSHIP_EDGE"
    ]

    assert len(duplicates) == 1
    assert duplicates[0].severity == "warning"
    assert report.is_ok  # a warning must not make the package invalid

    # The report joins the type back to a readable name: the stored column is
    # an id, and a report printing that would be useless to a human.
    assert duplicates[0].details["relationship_type"] == "boundary"
    assert duplicates[0].details["from_city_object_id"] == parent


def test_validation_catches_unsupported_encoding(tmp_path: Path) -> None:
    db_path = tmp_path / "bad_encoding.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET encoding = 'unknown-encoding'
                WHERE membership_block_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "UNSUPPORTED_MEMBERSHIP_ENCODING" in _codes(report)

def test_validate_connection_accepts_plain_connection(tmp_path: Path) -> None:
    # Validation must work on a bare sqlite3.Connection and restore the
    # caller's row factory, not hijack it.
    db_path = tmp_path / "plain.usap.gpkg"

    with make_pkg(tmp_path, "plain.usap.gpkg"):
        pass

    conn = sqlite3.connect(db_path)

    try:
        report = validate_connection(conn)

        assert report.is_ok, [issue.format() for issue in report.issues]
        assert conn.row_factory is None
    finally:
        conn.close()


def test_basic_level_skips_payload_decoding(tmp_path: Path) -> None:
    # 'basic' exists so a package with millions of membership blocks can be
    # checked without reading every blob off disk. The proof that it really
    # skips them: a corrupt payload that 'deep' reports must go unnoticed,
    # while the structural checks still run.
    db_path = tmp_path / "levels.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                UPDATE usap_membership_block
                SET payload = X'000102'
                WHERE membership_block_id = 1
                """
            )

        assert "CORRUPT_MEMBERSHIP_PAYLOAD" in _codes(pkg.validate_report())
        assert pkg.validate_report(level="basic").is_ok


def test_unknown_validation_level_is_refused(tmp_path: Path) -> None:
    db_path = tmp_path / "level_name.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pytest.raises(USAPError, match="Unknown validation level"):
            pkg.validate_report(level="thorough")


def test_external_level_detects_changed_asset(tmp_path: Path) -> None:
    # An annotation is bound to one immutable version of a file by element
    # index. If the file changed, those indices may now name different
    # elements and nothing inside the package can tell — only re-hashing can,
    # which is why it is a level of its own rather than always-on.
    asset_path = tmp_path / "cloud.las"
    asset_path.write_bytes(b"original bytes")

    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(
            uri=str(asset_path),
            asset_kind="pointcloud",
            content_hash=sha256_file(asset_path),
        )

        assert pkg.validate_report(level="external").is_ok

        asset_path.write_bytes(b"different bytes entirely")

        report = pkg.validate_report(level="external")

        assert not report.is_ok
        assert "ASSET_FILE_CHANGED" in _codes(report)

        # Nothing inside the database changed, so the cheaper levels cannot
        # and must not claim to notice.
        assert pkg.validate_report().is_ok

        asset_path.unlink()

        assert "ASSET_FILE_MISSING" in _codes(pkg.validate_report(level="external"))


def test_verify_assets_reports_unhashed_assets(tmp_path: Path) -> None:
    # compute_hash=False is a legitimate choice for a 10 GB file, but it
    # trades away change detection; that has to be visible, not implied.
    asset_path = tmp_path / "big.las"
    asset_path.write_bytes(b"payload")

    with make_pkg(tmp_path) as pkg:
        pkg.register_asset(uri=str(asset_path), asset_kind="pointcloud")

        assert [item["status"] for item in verify_assets(pkg.conn)] == ["unhashed"]

        report = pkg.validate_report(level="external")

        assert report.is_ok
        assert "ASSET_NOT_HASHED" in _codes(report)

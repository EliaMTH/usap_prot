from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import make_pkg
from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
    validate_connection,
)


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


def test_validation_catches_missing_city_object_closure(tmp_path: Path) -> None:
    db_path = tmp_path / "missing_closure.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_city_object_closure
                WHERE graph_name = 'usap_default'
                  AND depth = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MISSING_CITY_OBJECT_CLOSURE_DIRECT" in _codes(report)


def test_validation_catches_missing_city_object_self_closure(
    tmp_path: Path,
) -> None:
    # The self-row (depth 0) check is what makes an object visible to
    # graph-scoped queries; it caught the carrier-creation bug, so it gets
    # its own corruption test.
    db_path = tmp_path / "missing_self_closure.usap.gpkg"

    _create_small_valid_package(db_path)

    with USAPPackage.open(db_path) as pkg:
        with pkg.transaction():
            pkg.conn.execute(
                """
                DELETE FROM usap_city_object_closure
                WHERE graph_name = 'usap_default'
                  AND depth = 0
                  AND ancestor_city_object_id = 1
                """
            )

        report = pkg.validate_report()

        assert not report.is_ok
        assert "MISSING_CITY_OBJECT_SELF_CLOSURE" in _codes(report)


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

    pkg.link_city_objects(
        parent_city_object_id=parent,
        child_city_object_id=child,
        relationship_type="boundedBy",
        role="roof",
    )

    # Simulate a legacy duplicate behind the API's back.
    with pkg.transaction():
        pkg.conn.execute(
            """
            INSERT INTO usap_city_object_relationship (
                graph_name, parent_city_object_id, child_city_object_id,
                relationship_type, role
            )
            VALUES ('usap_default', ?, ?, 'boundedBy', 'roof')
            """,
            (parent, child),
        )

    report = pkg.validate_report()
    duplicates = [
        issue for issue in report.issues
        if issue.code == "DUPLICATE_RELATIONSHIP_EDGE"
    ]

    assert len(duplicates) == 1
    assert duplicates[0].severity == "warning"
    assert report.is_ok  # a warning must not make the package invalid


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

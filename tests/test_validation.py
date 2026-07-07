from __future__ import annotations

from pathlib import Path

from usap import (
    ELEMENT_KIND_FACE,
    SyntheticConfig,
    USAPPackage,
    create_synthetic_package,
)


def _create_small_valid_package(db_path: Path):
    return create_synthetic_package(
        db_path,
        schema_path="sql/schema.sql",
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
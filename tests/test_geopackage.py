from __future__ import annotations

from pathlib import Path

from usap import (
    GPKG_APPLICATION_ID,
    GPKG_USER_VERSION,
    USAP_EXTENSION_NAME,
    USAPPackage,
    read_geopackage_header,
)


def test_created_package_has_geopackage_header(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        header = read_geopackage_header(pkg.conn)

        assert header["application_id"] == GPKG_APPLICATION_ID
        assert header["user_version"] == GPKG_USER_VERSION


def test_created_package_has_gpkg_core_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        tables = {
            row["name"]
            for row in pkg.conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }

        assert "gpkg_spatial_ref_sys" in tables
        assert "gpkg_contents" in tables
        assert "gpkg_extensions" in tables


def test_created_package_has_default_srs_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        srs_ids = {
            int(row["srs_id"])
            for row in pkg.conn.execute(
                """
                SELECT srs_id
                FROM gpkg_spatial_ref_sys
                """
            ).fetchall()
        }

        assert -1 in srs_ids
        assert 0 in srs_ids
        assert 4326 in srs_ids


def test_usap_tables_are_registered_as_extension(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        rows = pkg.conn.execute(
            """
            SELECT table_name, extension_name, scope
            FROM gpkg_extensions
            WHERE extension_name = ?
            """,
            (USAP_EXTENSION_NAME,),
        ).fetchall()

        table_names = {row["table_name"] for row in rows}

        assert "usap_profile" in table_names
        assert "usap_asset" in table_names
        assert "usap_membership_block" in table_names

        for row in rows:
            assert row["extension_name"] == USAP_EXTENSION_NAME
            assert row["scope"] == "read-write"


def test_validation_checks_geopackage_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "test.usap.gpkg"

    with USAPPackage.create(
        db_path,
        schema_path="sql/schema.sql",
        overwrite=True,
    ) as pkg:
        report = pkg.validate_report()

        assert report.is_ok
        assert report.issues == []